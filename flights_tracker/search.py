"""Google Flights lookups (via fast-flights) with a same-day disk cache."""

import hashlib
import json
import random
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from .config import MAX_JOURNEY_MINUTES, MAX_STOPS

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"


@dataclass
class Segment:
    origin: str
    dest: str
    depart: str  # local time, ISO format
    arrive: str  # local time, ISO format
    minutes: int


@dataclass
class Leg:
    """One priced one-way (or round-trip, see `round_trip`) option."""

    origin: str
    dest: str
    price: int  # GBP
    airlines: list[str]
    segments: list[Segment]
    round_trip: bool = False

    @property
    def stops(self) -> int:
        return len(self.segments) - 1

    @property
    def depart(self) -> datetime:
        return datetime.fromisoformat(self.segments[0].depart)

    @property
    def arrive(self) -> datetime:
        return datetime.fromisoformat(self.segments[-1].arrive)

    @property
    def journey_minutes(self) -> int:
        # Flight times plus layovers. A layover's two timestamps are local to the
        # same airport, so subtracting them is timezone-safe.
        total = sum(s.minutes for s in self.segments)
        for prev, nxt in zip(self.segments, self.segments[1:]):
            gap = datetime.fromisoformat(nxt.depart) - datetime.fromisoformat(prev.arrive)
            total += int(gap.total_seconds() // 60)
        return total

    @property
    def via(self) -> str:
        return ", ".join(s.dest for s in self.segments[:-1]) or "direct"

    def ok(self, max_stops: int = MAX_STOPS, max_minutes: int = MAX_JOURNEY_MINUTES) -> bool:
        return self.price > 0 and self.stops <= max_stops and self.journey_minutes <= max_minutes

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Leg":
        return cls(**{**d, "segments": [Segment(**s) for s in d["segments"]]})


def _iso(sdt) -> str:
    y, m, d = sdt.date
    hh, mm = sdt.time
    return datetime(y, m, d, hh, mm).isoformat()


def _to_legs(results, round_trip: bool) -> list[Leg]:
    legs = []
    for r in results:
        if not r.flights:
            continue
        legs.append(
            Leg(
                origin=r.flights[0].from_airport.code,
                dest=r.flights[-1].to_airport.code,
                price=int(r.price or 0),
                airlines=list(r.airlines),
                segments=[
                    Segment(
                        origin=s.from_airport.code,
                        dest=s.to_airport.code,
                        depart=_iso(s.departure),
                        arrive=_iso(s.arrival),
                        minutes=int(s.duration or 0),
                    )
                    for s in r.flights
                ],
                round_trip=round_trip,
            )
        )
    return legs


class Searcher:
    def __init__(self, use_cache: bool = True, delay: float = 2.0, retries: int = 3):
        self.use_cache = use_cache
        self.delay = delay
        self.retries = retries
        self.queries = 0
        self.failures: list[str] = []
        CACHE_DIR.mkdir(exist_ok=True)

    def _cache_path(self, key: str) -> Path:
        # Prices move, so the cache only lives for the day it was fetched.
        digest = hashlib.sha1(f"{date.today()}|{key}".encode()).hexdigest()[:16]
        return CACHE_DIR / f"{digest}.json"

    def _fetch(self, key: str, query, round_trip: bool) -> list[Leg]:
        from fast_flights import FlightsNotFound, get_flights

        path = self._cache_path(key)
        if self.use_cache and path.exists():
            return [Leg.from_dict(d) for d in json.loads(path.read_text())]

        for attempt in range(1, self.retries + 1):
            try:
                self.queries += 1
                legs = _to_legs(get_flights(query), round_trip)
                break
            except FlightsNotFound:
                legs = []
                break
            except Exception as e:  # network hiccups, rate limiting, parse changes
                if attempt == self.retries:
                    self.failures.append(f"{key}: {e}")
                    return []
                time.sleep(self.delay * 2**attempt)
        time.sleep(self.delay + random.random())
        path.write_text(json.dumps([leg.to_dict() for leg in legs]))
        return legs

    def one_way(self, origin: str, dest: str, day: date, max_stops: int = MAX_STOPS) -> list[Leg]:
        from fast_flights import FlightQuery, create_query

        q = create_query(
            flights=[
                FlightQuery(
                    date=day.isoformat(),
                    from_airport=origin,
                    to_airport=dest,
                    max_stops=max_stops,
                    max_duration_minutes=MAX_JOURNEY_MINUTES,
                )
            ],
            trip="one-way",
            currency="GBP",
            language="en-GB",
        )
        return self._fetch(f"ow|{origin}|{dest}|{day}|{max_stops}", q, round_trip=False)

    def round_trip(self, origin: str, dest: str, out_day: date, back_day: date) -> list[Leg]:
        """Round-trip fares. Google returns the outbound leg with the total price;
        the return leg is picked on the next page, so it is not checked here."""
        from fast_flights import FlightQuery, create_query

        q = create_query(
            flights=[
                FlightQuery(date=out_day.isoformat(), from_airport=origin, to_airport=dest,
                            max_stops=MAX_STOPS, max_duration_minutes=MAX_JOURNEY_MINUTES),
                FlightQuery(date=back_day.isoformat(), from_airport=dest, to_airport=origin,
                            max_stops=MAX_STOPS, max_duration_minutes=MAX_JOURNEY_MINUTES),
            ],
            trip="round-trip",
            currency="GBP",
            language="en-GB",
        )
        return self._fetch(f"rt|{origin}|{dest}|{out_day}|{back_day}", q, round_trip=True)

    @staticmethod
    def url(origin: str, dest: str, day: date) -> str:
        from fast_flights import FlightQuery, create_query

        q = create_query(
            flights=[FlightQuery(date=day.isoformat(), from_airport=origin, to_airport=dest,
                                 max_stops=MAX_STOPS)],
            trip="one-way", currency="GBP", language="en-GB",
        )
        return q.url()
