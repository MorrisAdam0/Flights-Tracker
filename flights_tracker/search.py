"""Google Flights lookups (via fast-flights) with a same-day disk cache.

Every search is also logged as an observation (cheapest price seen, plus Google's own
price-history series when the page carries one) so prices can be tracked over time.
"""

import hashlib
import json
import random
import statistics
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"


@dataclass(frozen=True)
class Spec:
    """One Google Flights search."""

    origin: str
    dest: str
    day: date
    back: date | None = None  # set for round trips
    max_stops: int = 1
    max_minutes: int | None = None

    @property
    def key(self) -> str:
        kind = "rt" if self.back else "ow"
        return f"{kind}|{self.origin}|{self.dest}|{self.day}|{self.back or ''}|{self.max_stops}|{self.max_minutes or ''}"

    @classmethod
    def from_key(cls, key: str) -> "Spec":
        _, o, d, day, back, stops, mins = key.split("|")
        return cls(o, d, date.fromisoformat(day), date.fromisoformat(back) if back else None,
                   int(stops), int(mins) if mins else None)

    def query(self):
        from fast_flights import FlightQuery, create_query

        def fq(day, a, b):
            return FlightQuery(date=day.isoformat(), from_airport=a, to_airport=b,
                               max_stops=self.max_stops, max_duration_minutes=self.max_minutes)

        flights = [fq(self.day, self.origin, self.dest)]
        if self.back:
            flights.append(fq(self.back, self.dest, self.origin))
        return create_query(flights=flights, trip="round-trip" if self.back else "one-way",
                            currency="GBP", language="en-GB")

    def url(self) -> str:
        return self.query().url()


@dataclass
class Segment:
    origin: str
    dest: str
    depart: str  # local time, ISO format
    arrive: str  # local time, ISO format
    minutes: int


@dataclass
class Leg:
    """One priced flight option. For a round trip, `price` is the whole return fare and
    `segments` are the outbound only (Google shows the return on its next page)."""

    origin: str
    dest: str
    price: int  # GBP
    airlines: list[str]
    segments: list[Segment]
    spec_key: str = ""

    @property
    def spec(self) -> Spec:
        return Spec.from_key(self.spec_key)

    @property
    def round_trip(self) -> bool:
        return self.spec_key.startswith("rt|")

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
        return ", ".join(s.dest for s in self.segments[:-1]) or "nonstop"

    @property
    def signature(self) -> str:
        """Identifies the same flights across days, so a price can be tracked."""
        return "|".join(f"{s.origin}{s.depart}{s.dest}" for s in self.segments) + "|" + "/".join(self.airlines)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Leg":
        return cls(**{**d, "segments": [Segment(**s) for s in d["segments"]]})


@dataclass
class Observation:
    spec_key: str
    min_price: int | None
    options: int
    google: dict = field(default_factory=dict)  # summary of Google's price-history series


def _iso(sdt) -> str:
    y, m, d = sdt.date
    hh, mm = sdt.time
    return datetime(y, m, d, hh, mm).isoformat()


def _to_legs(results, spec_key: str) -> list[Leg]:
    legs = []
    for r in results:
        if not r.flights or not r.price:
            continue
        legs.append(Leg(
            origin=r.flights[0].from_airport.code,
            dest=r.flights[-1].to_airport.code,
            price=int(r.price),
            airlines=list(r.airlines),
            segments=[
                Segment(s.from_airport.code, s.to_airport.code, _iso(s.departure), _iso(s.arrival),
                        int(s.duration or 0))
                for s in r.flights
            ],
            spec_key=spec_key,
        ))
    return legs


def find_price_series(payload) -> list[tuple[int, float]]:
    """Google's "price history" chart is a list of [epoch_ms, price] points somewhere in
    the page payload. Its position isn't documented, so look for the longest such list."""
    best: list = []
    stack = [payload]
    while stack:
        node = stack.pop()
        if not isinstance(node, list):
            continue
        if len(node) >= 10 and all(
            isinstance(p, list) and len(p) >= 2
            and isinstance(p[0], int) and 1.5e12 < p[0] < 2.2e12
            and isinstance(p[1], (int, float)) and p[1] > 0
            for p in node
        ):
            if len(node) > len(best):
                best = node
            continue
        stack.extend(node)
    return [(p[0], p[1]) for p in best]


def summarise_series(series: list[tuple[int, float]]) -> dict:
    if not series:
        return {}
    series = sorted(series)
    prices = [p for _, p in series]
    recent = series[-14:]
    slope = 0.0
    if len(recent) >= 2:
        span_days = (recent[-1][0] - recent[0][0]) / 86_400_000 or 1
        slope = (recent[-1][1] - recent[0][1]) / span_days
    return {
        "from": datetime.fromtimestamp(series[0][0] / 1000).date().isoformat(),
        "points": len(series),
        "min": min(prices),
        "median": statistics.median(prices),
        "max": max(prices),
        "first": prices[0],
        "last": prices[-1],
        "slope_14d": round(slope, 2),  # GBP per day over the last two weeks
    }


def parse_page(html: str, spec_key: str) -> tuple[list[Leg], dict]:
    from fast_flights.parser import parse_js
    from selectolax.lexbor import LexborHTMLParser

    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None:
        raise RuntimeError("no results payload in page (blocked or consent page?)")
    js = script.text()
    google = {}
    try:
        payload = json.loads(js.split("data:", 1)[1].rsplit(",", 1)[0])
        google = summarise_series(find_price_series(payload))
    except Exception:
        pass
    try:
        legs = _to_legs(parse_js(js), spec_key)
    except Exception as e:
        if "FlightsNotFound" in type(e).__name__ or "errorHasStatus" in js:
            legs = []
        else:
            raise
    return legs, google


class Searcher:
    def __init__(self, use_cache: bool = True, delay: float = 2.0, retries: int = 3):
        self.use_cache = use_cache
        self.delay = delay
        self.retries = retries
        self.queries = 0
        self.failures: list[str] = []
        self.observations: dict[str, Observation] = {}
        self._lock = threading.Lock()
        CACHE_DIR.mkdir(exist_ok=True)

    def _cache_path(self, key: str) -> Path:
        # Prices move, so the cache only lives for the day it was fetched.
        digest = hashlib.sha1(f"{date.today()}|{key}".encode()).hexdigest()[:16]
        return CACHE_DIR / f"{digest}.json"

    def _download(self, spec: Spec) -> str:
        from fast_flights import fetch_flights_html

        return fetch_flights_html(spec.query())

    def search(self, spec: Spec) -> list[Leg]:
        key = spec.key
        path = self._cache_path(key)
        if self.use_cache and path.exists():
            cached = json.loads(path.read_text())
            legs, google = [Leg.from_dict(d) for d in cached["legs"]], cached["google"]
        else:
            for attempt in range(1, self.retries + 1):
                try:
                    with self._lock:
                        self.queries += 1
                    legs, google = parse_page(self._download(spec), key)
                    break
                except Exception as e:  # network hiccups, rate limiting, page changes
                    if attempt == self.retries:
                        with self._lock:
                            self.failures.append(f"{key}: {e}")
                        return []
                    time.sleep(self.delay * 2**attempt)
            time.sleep(self.delay + random.random())
            path.write_text(json.dumps({"legs": [l.to_dict() for l in legs], "google": google}))
        with self._lock:
            self.observations[key] = Observation(
                key, min((l.price for l in legs), default=None), len(legs), google)
        return legs

    def one_way(self, origin, dest, day, max_stops=1, max_minutes=None) -> list[Leg]:
        return self.search(Spec(origin, dest, day, None, max_stops, max_minutes))

    def round_trip(self, origin, dest, day, back, max_stops=1, max_minutes=None) -> list[Leg]:
        return self.search(Spec(origin, dest, day, back, max_stops, max_minutes))
