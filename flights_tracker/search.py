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

from .fetch import BrowserFetcher, HttpFetcher, PageError, browser_available, check_page, classify, results_script, save_debug

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
    from fast_flights.exceptions import FlightsNotFound
    from fast_flights.parser import parse_js

    js = results_script(html)
    if js is None:
        raise PageError(classify(html) or "no_payload")
    google = {}
    try:
        payload = json.loads(js.split("data:", 1)[1].rsplit(",", 1)[0])
        google = summarise_series(find_price_series(payload))
    except Exception:
        pass
    try:
        legs = _to_legs(parse_js(js), spec_key)
    except FlightsNotFound:
        legs = []  # a real "no flights on this route/date" answer
    except Exception as e:
        raise PageError("parse_error", f"{type(e).__name__}: {e}") from e
    return legs, google


class SearchAborted(Exception):
    pass


ADVICE = {
    "consent": "Google showed its cookie-consent page instead of results. Install the browser fallback: "
               "pip install playwright && playwright install chromium, then run with --fetcher browser.",
    "captcha": "Google is showing an 'unusual traffic' check to this IP. Wait an hour or two, use fewer "
               "--workers and a bigger --delay, or run from another network.",
    "rate_limited": "Google is rate-limiting this IP. Wait a while and retry with fewer --workers and a bigger --delay.",
    "no_payload": "The page had no results data. Google may have changed the page; try --fetcher browser.",
    "parse_error": "The results data has changed shape; fast-flights may need updating (pip install -U fast-flights).",
    "http_error": "Network/HTTP errors. Check your connection or proxy.",
}


class Searcher:
    # Stop the run instead of hammering Google when nothing is getting through.
    ABORT_IF_FIRST_FAIL = 6
    ABORT_AFTER_CONSECUTIVE = 30

    def __init__(self, use_cache: bool = True, delay: float = 2.0, retries: int = 3, fetcher: str = "auto"):
        self.use_cache = use_cache
        self.delay = delay
        self.retries = retries
        self.mode = fetcher
        self.fetcher = BrowserFetcher() if fetcher == "browser" else HttpFetcher()
        self.queries = 0
        self.successes = 0
        self.consecutive_failures = 0
        self.failure_kinds: dict[str, int] = {}
        self.failures: list[str] = []
        self.aborted: str | None = None
        self.debug_pages: list[Path] = []
        self.observations: dict[str, Observation] = {}
        self._lock = threading.Lock()
        CACHE_DIR.mkdir(exist_ok=True)

    def _cache_path(self, key: str) -> Path:
        # Prices move, so the cache only lives for the day it was fetched.
        digest = hashlib.sha1(f"{date.today()}|{key}".encode()).hexdigest()[:16]
        return CACHE_DIR / f"{digest}.json"

    def _download(self, spec: Spec) -> str:
        html, url, status = self.fetcher.fetch(spec.query().params())
        try:
            return check_page(html, url, status)
        except PageError:
            with self._lock:
                if len(self.debug_pages) < 3:
                    self.debug_pages.append(save_debug(f"{self.fetcher.name}_{spec.key}", html))
            raise

    def _switch_to_browser(self, kind: str) -> bool:
        """In auto mode, move to the headless browser the first time plain requests are refused."""
        with self._lock:
            if self.fetcher.name == "browser":
                return self.mode == "auto"  # another thread already switched
            if self.mode != "auto" or kind not in ("consent", "captcha", "no_payload") or not browser_available():
                return False
            print(f"plain requests got '{kind}' pages; switching to the headless browser")
            self.fetcher = BrowserFetcher()
            return True

    def _failed(self, key: str, err: Exception) -> None:
        kind = getattr(err, "kind", "http_error")
        with self._lock:
            self.failures.append(f"{key}: {err}")
            self.failure_kinds[kind] = self.failure_kinds.get(kind, 0) + 1
            self.consecutive_failures += 1
            if self.aborted is None and (
                (self.successes == 0 and self.consecutive_failures >= self.ABORT_IF_FIRST_FAIL)
                or self.consecutive_failures >= self.ABORT_AFTER_CONSECUTIVE
            ):
                self.aborted = f"{self.consecutive_failures} searches in a row failed ({kind}). {ADVICE.get(kind, '')}"
                if self.debug_pages:
                    self.aborted += f" A failing page was saved to {self.debug_pages[0]}."

    def search(self, spec: Spec) -> list[Leg]:
        key = spec.key
        path = self._cache_path(key)
        if self.use_cache and path.exists():
            cached = json.loads(path.read_text())
            legs, google = [Leg.from_dict(d) for d in cached["legs"]], cached["google"]
        else:
            attempt, switched = 0, False
            while True:
                if self.aborted:
                    raise SearchAborted(self.aborted)
                attempt += 1
                try:
                    with self._lock:
                        self.queries += 1
                    legs, google = parse_page(self._download(spec), key)
                    break
                except Exception as e:  # network hiccups, rate limiting, consent pages, page changes
                    err = e if isinstance(e, PageError) else PageError("http_error", str(e))
                    if not err.retryable and not switched and self._switch_to_browser(err.kind):
                        switched = True  # one more go with the headless browser
                        continue
                    if not err.retryable or attempt >= self.retries:
                        self._failed(key, err)
                        if self.aborted:
                            raise SearchAborted(self.aborted)
                        return []
                    time.sleep(30 * attempt if err.kind == "rate_limited" else self.delay * 2**attempt)
            with self._lock:
                self.successes += 1
                self.consecutive_failures = 0
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
