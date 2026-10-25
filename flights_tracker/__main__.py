"""python -m flights_tracker [--trip mexico|malaysia|all] [--track]"""

import argparse

from . import history, report
from .config import TRIPS
from .planner import plan
from .search import Searcher

WATCH_PER_TRIP = 25


def main() -> None:
    ap = argparse.ArgumentParser(description="Brute-force the cheapest way to Mexico or Malaysia and back.")
    ap.add_argument("--trip", choices=[*TRIPS, "all"], default="all")
    ap.add_argument("--track", action="store_true",
                    help="only re-price the shortlist from the last full search (fast; for daily runs)")
    ap.add_argument("--top", type=int, default=20, help="rows per trip in the report")
    ap.add_argument("--workers", type=int, default=3, help="parallel searches (keep low to avoid rate limits)")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between searches per worker")
    ap.add_argument("--no-cache", action="store_true", help="ignore today's cached results")
    args = ap.parse_args()

    searcher = Searcher(use_cache=not args.no_cache, delay=args.delay)
    names = list(TRIPS) if args.trip == "all" else [args.trip]
    watch = history.load_watchlist()
    if args.track and not watch:
        ap.error("no shortlist yet: run a full search first")

    results = {}
    for key in names:
        trip = TRIPS[key]
        if args.track:
            results[trip.name] = history.reprice([i for i in watch if i.trip == trip.name], searcher)
        else:
            results[trip.name] = plan(trip, searcher, workers=args.workers)

    # Record history before writing the report, so the report includes today's prices.
    history.record_observations(searcher)
    shortlist = [i for itins in results.values() for i in itins[:WATCH_PER_TRIP]]
    history.record_itineraries(shortlist)
    if not args.track:
        # Keep the old shortlist for trips not searched, or whose searches all failed.
        found = {name for name, itins in results.items() if itins}
        history.save_watchlist(shortlist + [i for i in watch if i.trip not in found])

    sections = []
    for name, itins in results.items():
        sections.append(report.trip_section(name, itins, args.top, args.track))
        if not args.track:
            report.write_itineraries_csv(name, itins)
        if itins:
            print(f"[{name}] cheapest £{itins[0].total}: {history.route_label(itins[0])}")
    path = report.save(sections, searcher.failures, args.track)
    print(f"{searcher.queries} live searches, {len(searcher.failures)} failed. Report: {path}")


if __name__ == "__main__":
    main()
