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


def _time(value) -> tuple[int, int]:
    # Google drops zero components: [8] is 08:00 and [None, 31] is 00:31.
    padded = [*(value or []), None, None]
    return padded[0] or 0, padded[1] or 0


def _segment(raw) -> Segment:
    y, m, d = raw[20]
    y2, m2, d2 = raw[21]
    dep, arr = _time(raw[8]), _time(raw[10])
    return Segment(raw[3], raw[6], datetime(y, m, d, *dep).isoformat(), datetime(y2, m2, d2, *arr).isoformat(),
                   int(raw[11] or 0))


def legs_from_payload(payload, spec_key: str) -> list[Leg]:
    """Flights from the results payload. Written here rather than using fast-flights' parser,
    which crashes on pages with no flights (no airline metadata) and only reads one list."""
    legs, seen = [], set()
    groups = []
    for idx in (2, 3):  # Google's "best" and "other" flights
        try:
            groups.append(payload[idx][0] or [])
        except (IndexError, TypeError):
            pass
    for group in groups:
        for item in group:
            try:
                flight, price = item[0], item[1][0][1]
                segments = [_segment(raw) for raw in flight[2]]
            except (IndexError, TypeError, ValueError):
                continue  # an entry without a price or in an unexpected shape
            if not price or not segments:
                continue
            leg = Leg(segments[0].origin, segments[-1].dest, int(price), list(flight[1] or []), segments, spec_key)
            if (leg.signature, leg.price) not in seen:
                seen.add((leg.signature, leg.price))
                legs.append(leg)
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
    js = results_script(html)
    if js is None:
        raise PageError(classify(html) or "no_payload")
    if "errorHasStatus: true" in js[-300:]:
        return [], {}  # Google's own "no flights found" answer
    try:
        payload = json.loads(js.split("data:", 1)[1].rsplit(",", 1)[0])
    except (IndexError, ValueError) as e:
        raise PageError("parse_error", f"results data unreadable: {e}") from e
    if not isinstance(payload, list):
        # Pages served while Google is throttling can carry an empty payload.
        raise PageError("rate_limited", "empty results payload")
    google = {}
    try:
        google = summarise_series(find_price_series(payload))
    except Exception:
        pass
    return legs_from_payload(payload, spec_key), google


class SearchAborted(Exception):
    pass


THROTTLE_SIGNS = ("excessive load", "enhance_your_calm", "enhance your calm", "goaway", "too many requests")


def transport_error(e: Exception) -> PageError:
    msg = str(e)
    if any(sign in msg.lower() for sign in THROTTLE_SIGNS):
        return PageError("rate_limited", msg[:200])
    return PageError("http_error", msg[:200])


ADVICE = {
    "consent": "Google showed its cookie-consent page instead of results. Install the browser fallback: "
               "pip install playwright && playwright install chromium, then run with --fetcher browser.",
    "captcha": "Google is showing an 'unusual traffic' check to this IP. Wait an hour or two, use fewer "
               "--workers and a bigger --delay, or run from another network.",
    "rate_limited": "Google is rate-limiting this IP. Wait an hour or so and re-run (today's results are cached), "
                    "with --workers 1 --delay 5 if it keeps happening.",
    "no_payload": "The page had no results data. Google may have changed the page; try --fetcher browser.",
    "parse_error": "Google's results data wasn't in the expected shape. Please share the page saved in results/debug/.",
    "http_error": "Network/HTTP errors. Check your connection or proxy.",
}


class Searcher:
    # Stop the run instead of hammering Google when nothing is getting through.
    ABORT_IF_FIRST_FAIL = 6
    ABORT_AFTER_CONSECUTIVE = 30
    RATE_LIMIT_RETRIES = 5
    MAX_COOLDOWNS = 7  # 1, 2, 4, 8, 10, 10, 10 minutes, then give up for now

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
        self.cooldowns = 0
        self._cool_until = 0.0
        self._lock = threading.Lock()
        CACHE_DIR.mkdir(exist_ok=True)

    def _cache_path(self, key: str) -> Path:
        # Prices move, so the cache only lives for the day it was fetched.
        digest = hashlib.sha1(f"{date.today()}|{key}".encode()).hexdigest()[:16]
        return CACHE_DIR / f"{digest}.json"

    def _download(self, spec: Spec) -> tuple[list[Leg], dict]:
        html, url, status = self.fetcher.fetch(spec.query().params())
        try:
            return parse_page(check_page(html, url, status), spec.key)
        except PageError as e:
            # Keep a couple of examples of each kind of bad page, so the cause can be seen.
            with self._lock:
                if sum(e.kind in p.name for p in self.debug_pages) < 2:
                    self.debug_pages.append(save_debug(f"{e.kind}_{self.fetcher.name}_{spec.key}", html))
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

    def _cool_down(self, why: str) -> None:
        """Google is throttling: every worker pauses, then carries on with a fresh connection."""
        with self._lock:
            if time.time() < self._cool_until:
                return  # another worker already started this pause
            self.cooldowns += 1
            if self.cooldowns > self.MAX_COOLDOWNS:
                self.aborted = ("Google kept throttling after repeated pauses. Everything found so far is cached "
                                "for today: run the same command again later and it carries on where it stopped. "
                                + ADVICE["rate_limited"])
                return
            pause = min(60 * 2 ** (self.cooldowns - 1), 600)
            self._cool_until = time.time() + pause
            print(f"Google is throttling ({why[:60]}): pausing all searches for {pause // 60} min")
            if hasattr(self.fetcher, "reset"):
                self.fetcher.reset()

    def _wait_if_cooling(self) -> None:
        wait = self._cool_until - time.time()
        if wait > 0:
            time.sleep(wait + random.random() * 5)

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
                self._wait_if_cooling()
                if self.aborted:
                    raise SearchAborted(self.aborted)
                try:
                    with self._lock:
                        self.queries += 1
                    legs, google = self._download(spec)
                    break
                except Exception as e:  # network hiccups, rate limiting, consent pages, page changes
                    err = e if isinstance(e, PageError) else transport_error(e)
                    if err.kind == "rate_limited":
                        self._cool_down(str(err))
                        if attempt < self.RATE_LIMIT_RETRIES and not self.aborted:
                            continue
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
                if time.time() > self._cool_until + 300:
                    self.cooldowns = 0  # five calm minutes: reset the back-off
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
