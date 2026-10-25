"""python -m flights_tracker [--trip mexico|malaysia|all]"""

import argparse

from . import report
from .config import TRIPS
from .planner import plan
from .search import Searcher


def main() -> None:
    ap = argparse.ArgumentParser(description="Find the cheapest open-jaw/round-trip combos for a winter getaway.")
    ap.add_argument("--trip", choices=[*TRIPS, "all"], default="all")
    ap.add_argument("--top", type=int, default=15, help="rows per trip in the report")
    ap.add_argument("--workers", type=int, default=3, help="parallel searches (keep low to avoid rate limits)")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between searches per worker")
    ap.add_argument("--no-cache", action="store_true", help="ignore today's cached results")
    args = ap.parse_args()

    searcher = Searcher(use_cache=not args.no_cache, delay=args.delay)
    sections, legs_by_trip = [], {}
    for key in TRIPS if args.trip == "all" else [args.trip]:
        trip = TRIPS[key]
        combos, legs = plan(trip, searcher, workers=args.workers)
        sections.append(report.markdown(trip.name, combos, args.top))
        legs_by_trip[trip.name] = legs
        report.append_history(trip.name, combos)
        if combos:
            print(f"[{trip.name}] best: £{combos[0].total} ({combos[0].fly_in} → {combos[0].fly_out})")

    path = report.save(sections, legs_by_trip, searcher.failures)
    print(f"{searcher.queries} live searches, {len(searcher.failures)} failed. Report: {path}")


if __name__ == "__main__":
    main()
