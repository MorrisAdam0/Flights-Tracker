"""Markdown report and CSV export of ranked itineraries, with price-history verdicts."""

import csv
from datetime import datetime
from pathlib import Path

from . import config as C
from . import history as H
from .planner import Itinerary
from .search import Leg

RESULTS_DIR = H.RESULTS_DIR


def _hm(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}"


def _gbp(x) -> str:
    return f"£{x:,.0f}"


def _ticket_line(t: Leg) -> str:
    rt = f" (return ticket, back {t.spec.back:%a %d %b})" if t.round_trip else ""
    return (f"[{t.origin}→{t.dest}]({t.spec.url()}) {t.depart:%a %d %b %H:%M} → {t.arrive:%a %d %b %H:%M} · "
            f"{_hm(t.journey_minutes)} · {t.via} · {'/'.join(t.airlines)} · **{_gbp(t.price)}**{rt}")


def _insight_line(t: Leg, obs: list[dict]) -> str:
    ins = H.ticket_insight(t.spec_key, obs)
    g = ins.get("google")
    ours = ins["ours"]
    bits = []
    if g:
        bits.append(f"Google 60-day: low {_gbp(g['min'])}, typical {_gbp(g['median'])}, high {_gbp(g['max'])}, "
                    f"{g['slope_14d']:+.0f}/day lately")
    if len(ours) >= 2:
        bits.append(f"our checks: {_gbp(ours[0][1])} on {ours[0][0]:%d %b} → {_gbp(ours[-1][1])} now")
    bits.append(H.verdict(t.price, g, H.slope(ours[-14:])))
    return " · ".join(bits)


def _summary(it: Itinerary) -> str:
    return f"{H.route_label(it)} ({it.leave_london:%d %b}–{it.leave_country:%d %b})"


def trip_section(trip_name: str, itins: list[Itinerary], top: int, tracking: bool) -> str:
    obs, itin_rows = H.load_csv("observations.csv"), H.load_csv("itineraries.csv")
    lines = [f"## {trip_name}", ""]
    if not itins:
        return "\n".join(lines + ["No itineraries met the rules.", ""])

    best = itins[0]
    tr = H.itinerary_trend(best.id, itin_rows)
    lines.append(f"**Cheapest: {_gbp(best.total)}**: fly into {best.fly_in}, out of {best.fly_out}, {_summary(best)}.")
    if tr.get("runs", 0) >= 2:
        lines.append(f"Tracked {tr['runs']} times since {tr['first_seen']:%d %b}: {_gbp(tr['first'])} → "
                     f"{_gbp(tr['now'])} (low {_gbp(tr['low'])}, {tr['slope'] or 0:+.0f}/day over 2 weeks).")
    lines.append("")

    if not tracking:
        lines += ["**Cheapest by strategy**", "", "| Strategy | Total | Route | Dates |", "|---|---:|---|---|"]
        seen = set()
        for it in itins:
            if it.strategy in seen or it.ground_unknown:
                continue
            seen.add(it.strategy)
            lines.append(f"| {it.strategy} | {_gbp(it.total)} | {H.route_label(it)} "
                         f"| {it.leave_london:%a %d %b} – {it.leave_country:%a %d %b} |")
        lines.append("")

    # Same route on a different date adds little, so show each route once (its cheapest date).
    distinct, seen_routes = [], set()
    for it in itins:
        k = (H.route_label(it), it.fly_in, it.fly_out)
        if k not in seen_routes:
            seen_routes.add(k)
            distinct.append(it)
    lines += [
        f"**Top {min(top, len(distinct))} routes** (cheapest date for each; every date is in the CSV)",
        "",
        "| # | Total | Flights | Hub nights | In-country | In → Out | Route | Leave London | Leave country |",
        "|--:|------:|--------:|-----------:|-----------:|----------|-------|------|------|",
    ]
    for n, it in enumerate(distinct[:top], 1):
        lines.append(
            f"| {n} | **{_gbp(it.total)}** | {_gbp(it.flights_cost)} | "
            f"{f'{it.nights_in_hubs} ({_gbp(it.stopover_cost)})' if it.nights_in_hubs else '–'} | "
            f"{_gbp(it.ground_cost) + ('+?' if it.ground_unknown else '') if it.ground else '–'} | "
            f"{it.fly_in} → {it.fly_out} | {H.route_label(it)} | {it.leave_london:%a %d %b} | {it.leave_country:%a %d %b} |")
    lines += ["", "**Top 5 in detail** (links open the search on Google Flights)", ""]
    for n, it in enumerate(distinct[:5], 1):
        lines.append(f"{n}. **{_gbp(it.total)}** · {it.strategy} · in {it.fly_in}, out {it.fly_out}")
        for t in it.tickets:
            lines.append(f"   - {_ticket_line(t)}")
            lines.append(f"     - _{_insight_line(t, obs)}_")
        if it.nights_in_hubs:
            lines.append(f"   - {it.nights_in_hubs} night(s) in the hub at ~{_gbp(C.STOPOVER_NIGHT_GBP)}/night")
        for desc, cost in it.ground:
            lines.append(f"   - In-country: {desc} {_gbp(cost) if cost is not None else '(unknown)'}")
        for note in it.notes:
            lines.append(f"   - Note: {note}")
    lines.append("")
    return "\n".join(lines)


def write_itineraries_csv(trip_name: str, itins: list[Itinerary], limit: int = 300) -> None:
    path = RESULTS_DIR / f"{trip_name.lower()}_itineraries.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "total_gbp", "flights_gbp", "hub_nights_gbp", "in_country_gbp", "strategy",
                    "fly_in", "fly_out", "leave_london", "leave_country", "route", "tickets"])
        for n, it in enumerate(itins[:limit], 1):
            w.writerow([n, it.total, it.flights_cost, it.stopover_cost,
                        f"{it.ground_cost}{'+?' if it.ground_unknown else ''}", it.strategy, it.fly_in,
                        it.fly_out, it.leave_london, it.leave_country, H.route_label(it),
                        " | ".join(f"{t.origin}-{t.dest} {t.depart:%d%b %H:%M} {'/'.join(t.airlines)} £{t.price}"
                                   for t in it.tickets)])


def save(sections: list[str], failures: list[str], tracking: bool) -> Path:
    RESULTS_DIR.mkdir(exist_ok=True)
    mode = "Re-priced shortlist" if tracking else "Full search"
    header = [
        "# Winter getaway flight search",
        "",
        f"{mode}, {datetime.now():%d %b %Y %H:%M}. Per person, GBP, from Google Flights.",
        "",
        f"- Leave London {C.OUTBOUND_DATES[0]:%d}–{C.OUTBOUND_DATES[-1]:%d %b}; leave the destination "
        f"{C.RETURN_DATES[0]:%d}–{C.RETURN_DATES[-1]:%d %b}; home by {C.LATEST_ARRIVAL_HOME:%d %b}.",
        f"- Direct from London: ≤{C.MAX_STOPS} stop, ≤{C.MAX_JOURNEY_MINUTES // 60}h each way. Via a hub: separate "
        f"nonstop tickets, ≥{C.MIN_SELF_TRANSFER_MINUTES // 60}h to connect, up to {C.MAX_STOPOVER_DAYS} nights "
        f"there at ~£{C.STOPOVER_NIGHT_GBP}/night.",
        "- Totals include getting to the must-visit city (Mexico City / Kuala Lumpur) and on to your departure "
        "airport. Bus/train costs are estimates; in-country flights are live prices.",
        "- Separate tickets aren't protected if the first flight is late. Leave the buffer, or stay a night.",
        "",
    ]
    footer = []
    if failures:
        footer = ["", f"<details><summary>{len(failures)} searches failed</summary>", "",
                  *[f"- {f}" for f in failures[:200]], "", "</details>", ""]
    path = RESULTS_DIR / "latest.md"
    path.write_text("\n".join(header + sections + footer))
    return path
