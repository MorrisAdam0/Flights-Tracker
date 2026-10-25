"""Markdown report, full CSV of legs, and a price-history log for tracking."""

import csv
from datetime import date, datetime
from pathlib import Path

from .planner import Combo
from .search import Leg, Searcher

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


def _hm(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}"


def _leg_line(leg: Leg) -> str:
    return (
        f"{leg.origin}→{leg.dest} {leg.depart:%a %d %b %H:%M} → {leg.arrive:%a %d %b %H:%M} "
        f"· {_hm(leg.journey_minutes)} · {leg.via} · {'/'.join(leg.airlines)} · £{leg.price}"
    )


def markdown(trip_name: str, combos: list[Combo], top: int = 15) -> str:
    lines = [f"## {trip_name}", ""]
    if not combos:
        return "\n".join(lines + ["No itineraries met the rules (≤1 stop, ≤16h, home by 10 Jan).", ""])
    lines += [
        "| # | Total | In → Out | Out | Back | Nights | Ground | Book |",
        "|---|------:|----------|-----|------|-------:|--------|------|",
    ]
    for i, c in enumerate(combos[:top], 1):
        out, back = c.outbound, c.inbound
        book = f"[out]({Searcher.url(out.origin, out.dest, out.depart.date())})"
        if back:
            book += f" · [back]({Searcher.url(back.origin, back.dest, back.depart.date())})"
        lines.append(
            f"| {i} | **£{c.total}** | {c.fly_in} → {c.fly_out} "
            f"| {out.origin}→{out.dest} {out.depart:%a %d %b} £{out.price} "
            f"| {f'{back.origin}→{back.dest} {back.depart:%a %d %b} £{back.price}' if back else 'round trip'} "
            f"| {c.nights if c.nights is not None else '–'} "
            f"| {f'£{c.ground_cost} ' if c.ground_cost else ''}{c.ground_note or '–'} | {book} |"
        )
    lines += ["", "**Top 3 in detail**", ""]
    for i, c in enumerate(combos[:3], 1):
        lines.append(f"{i}. £{c.total} — fly into {c.fly_in}, out of {c.fly_out}")
        lines.append(f"   - Out:  {_leg_line(c.outbound)}")
        if c.inbound:
            lines.append(f"   - Back: {_leg_line(c.inbound)}")
        if c.ground_note:
            lines.append(f"   - Ground: {c.ground_note}" + (f" £{c.ground_cost}" if c.ground_cost else ""))
        for n in c.notes:
            lines.append(f"   - Note: {n}")
    lines.append("")
    return "\n".join(lines)


def write_legs_csv(path: Path, legs: list[Leg]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["origin", "dest", "depart", "arrive", "price_gbp", "stops", "via",
                    "journey", "airlines", "round_trip", "meets_rules"])
        for l in sorted(legs, key=lambda l: (l.origin, l.dest, l.depart, l.price)):
            w.writerow([l.origin, l.dest, l.depart, l.arrive, l.price, l.stops, l.via,
                        _hm(l.journey_minutes), "/".join(l.airlines), l.round_trip, l.ok()])


def append_history(trip_name: str, combos: list[Combo]) -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / "history.csv"
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["checked_at", "trip", "best_total_gbp", "fly_in", "fly_out", "out_date", "back_date"])
        if combos:
            c = combos[0]
            w.writerow([datetime.now().isoformat(timespec="minutes"), trip_name, c.total, c.fly_in,
                        c.fly_out, c.outbound.depart.date(), c.inbound.depart.date() if c.inbound else ""])


def save(sections: list[str], legs_by_trip: dict[str, list[Leg]], failures: list[str]) -> Path:
    RESULTS_DIR.mkdir(exist_ok=True)
    header = [
        "# Winter getaway flight search",
        "",
        f"Checked {datetime.now():%d %b %Y %H:%M}. Prices are per person in GBP from Google Flights.",
        "Rules: leave London 28–30 Dec, leave for home 8–10 Jan and land by 10 Jan, ≤1 stop, ≤16h door to door each way.",
        "Open-jaw combos are two one-way tickets; ground costs between different gateways are estimates.",
        "",
    ]
    footer = []
    if failures:
        footer = ["", f"<details><summary>{len(failures)} searches failed</summary>", "",
                  *[f"- {f}" for f in failures], "", "</details>", ""]
    report = RESULTS_DIR / "latest.md"
    report.write_text("\n".join(header + sections + footer))
    for name, legs in legs_by_trip.items():
        write_legs_csv(RESULTS_DIR / f"{name.lower()}_legs_{date.today()}.csv", legs)
    return report
