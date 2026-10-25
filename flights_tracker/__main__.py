"""python -m flights_tracker [--trip mexico|malaysia|all] [--track] [--check]"""

import argparse
import sys

from . import history, report
from .config import OUTBOUND_DATES, TRIPS
from .planner import plan
from .search import ADVICE, SearchAborted, Searcher, Spec

WATCH_PER_TRIP = 25


def check(searcher: Searcher) -> int:
    """One live search, with a plain-English diagnosis. Run this first."""
    spec = Spec("LHR", "CUN", OUTBOUND_DATES[0], max_stops=1)
    searcher.ABORT_IF_FIRST_FAIL = 99
    legs = searcher.search(spec)
    print(f"fetcher: {searcher.fetcher.name}")
    if searcher.failures:
        kind = next(iter(searcher.failure_kinds))
        print(f"FAILED: {searcher.failures[-1]}\n{ADVICE.get(kind, '')}")
        for p in searcher.debug_pages:
            print(f"page saved to {p}")
        return 1
    print(f"OK: {len(legs)} flights LHR→CUN on {spec.day:%d %b}, cheapest £{min(l.price for l in legs) if legs else '-'}")
    for l in sorted(legs, key=lambda l: l.price)[:3]:
        print(f"  £{l.price} {l.depart:%H:%M}→{l.arrive:%H:%M} {l.via} {'/'.join(l.airlines)}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Brute-force the cheapest way to Mexico or Malaysia and back.")
    ap.add_argument("--trip", choices=[*TRIPS, "all"], default="all")
    ap.add_argument("--track", action="store_true",
                    help="only re-price the shortlist from the last full search (fast; for daily runs)")
    ap.add_argument("--check", action="store_true", help="run one search to test the connection to Google, then stop")
    ap.add_argument("--fetcher", choices=["auto", "http", "browser"], default="auto",
                    help="auto: plain requests, switching to headless Chromium if Google blocks them")
    ap.add_argument("--thorough", action="store_true",
                    help="search every hub route on every date, instead of probing which routes exist first")
    ap.add_argument("--top", type=int, default=20, help="rows per trip in the report")
    ap.add_argument("--workers", type=int, default=2, help="parallel searches (keep low to avoid rate limits)")
    ap.add_argument("--delay", type=float, default=3.0, help="seconds between searches per worker")
    ap.add_argument("--no-cache", action="store_true", help="ignore today's cached results")
    args = ap.parse_args()

    searcher = Searcher(use_cache=not (args.no_cache or args.check), delay=args.delay, fetcher=args.fetcher)
    if args.check:
        sys.exit(check(searcher))

    names = list(TRIPS) if args.trip == "all" else [args.trip]
    watch = history.load_watchlist()
    if args.track and not any(i.trip.lower() in names for i in watch):
        print("no shortlist yet (or the last full search found nothing): running a full search instead")
        args.track = False

    results = {}
    try:
        for key in names:
            trip = TRIPS[key]
            if args.track:
                results[trip.name] = history.reprice([i for i in watch if i.trip == trip.name], searcher)
            else:
                results[trip.name] = plan(trip, searcher, workers=args.workers, thorough=args.thorough)
    except SearchAborted as e:
        # Keep the previous results, shortlist and history as they were.
        path = report.save([f"## Search stopped\n\n{e}\n"], searcher.failures, args.track)
        print(f"STOPPED: {e}\n{searcher.queries} live searches. Report: {path}")
        sys.exit(1)

    # Record history before writing the report, so the report includes today's prices.
    history.record_observations(searcher)
    shortlist = [i for itins in results.values() for i in itins[:WATCH_PER_TRIP]]
    history.record_itineraries(shortlist)
    found = {name for name, itins in results.items() if itins}
    if not args.track:
        # Keep the old shortlist for trips not searched, or that found nothing.
        history.save_watchlist(shortlist + [i for i in watch if i.trip not in found])

    sections = []
    for name, itins in results.items():
        sections.append(report.trip_section(name, itins, args.top, args.track))
        if name in found and not args.track:
            report.write_itineraries_csv(name, itins)
        if itins:
            print(f"[{name}] cheapest £{itins[0].total}: {history.route_label(itins[0])}")
    path = report.save(sections, searcher.failures, args.track)
    print(f"{searcher.queries} live searches, {len(searcher.failures)} failed. Report: {path}")


if __name__ == "__main__":
    main()
