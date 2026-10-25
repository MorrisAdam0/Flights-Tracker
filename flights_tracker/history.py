"""Price history: what we've seen on each run, plus Google's own 60-day history where available.

results/history/observations.csv  one row per search per run (cheapest price + Google's history summary)
results/history/itineraries.csv   one row per shortlisted itinerary per run (its total)
results/watchlist.json            the shortlist that `--track` re-prices between full searches
"""

import csv
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

from .planner import Itinerary, direct_ok
from .search import Leg, Searcher, Spec

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
HISTORY_DIR = RESULTS_DIR / "history"
WATCHLIST = RESULTS_DIR / "watchlist.json"

OBS_FIELDS = ["checked_on", "spec", "min_price", "options",
              "g_from", "g_points", "g_min", "g_median", "g_max", "g_last", "g_slope_14d"]
ITIN_FIELDS = ["checked_on", "trip", "id", "strategy", "route", "total"]


def _append(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    existing = []
    if path.exists():
        # Re-running on the same day replaces that day's rows rather than duplicating them.
        keys = {(r["spec"] if "spec" in r else r["id"]) for r in rows}
        with path.open() as f:
            existing = [r for r in csv.DictReader(f)
                        if not (r["checked_on"] == today and r.get("spec", r.get("id")) in keys)]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fields)
        w.writeheader()
        w.writerows(existing + [{"checked_on": today, **r} for r in rows])


def record_observations(searcher: Searcher) -> None:
    rows = []
    for o in searcher.observations.values():
        g = o.google
        rows.append({"spec": o.spec_key, "min_price": o.min_price if o.min_price is not None else "",
                     "options": o.options, "g_from": g.get("from", ""), "g_points": g.get("points", ""),
                     "g_min": g.get("min", ""), "g_median": g.get("median", ""), "g_max": g.get("max", ""),
                     "g_last": g.get("last", ""), "g_slope_14d": g.get("slope_14d", "")})
    _append(HISTORY_DIR / "observations.csv", OBS_FIELDS, rows)


def route_label(it: Itinerary) -> str:
    return " + ".join(f"{t.origin}→{t.dest}{'⇄' if t.round_trip else ''}" for t in it.tickets)


def record_itineraries(itins: list[Itinerary]) -> None:
    _append(HISTORY_DIR / "itineraries.csv", ITIN_FIELDS,
            [{"trip": i.trip, "id": i.id, "strategy": i.strategy, "route": route_label(i), "total": i.total}
             for i in itins])


def load_csv(name: str) -> list[dict]:
    path = HISTORY_DIR / name
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


# ---------- watchlist: the shortlist re-priced daily ----------

def save_watchlist(itins: list[Itinerary]) -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    WATCHLIST.write_text(json.dumps([i.to_dict() for i in itins], indent=1))


def load_watchlist() -> list[Itinerary]:
    if not WATCHLIST.exists():
        return []
    return [Itinerary.from_dict(d) for d in json.loads(WATCHLIST.read_text())]


def reprice(itins: list[Itinerary], searcher: Searcher, log=print) -> list[Itinerary]:
    """Re-search just the tickets in the shortlist. Same flights where still on sale,
    otherwise the cheapest equivalent ticket in the same search."""
    specs = {t.spec_key for i in itins for t in i.tickets}
    log(f"re-pricing {len(itins)} itineraries ({len(specs)} searches)")
    fresh = {k: searcher.search(Spec.from_key(k)) for k in sorted(specs)}
    out = []
    for it in itins:
        tickets, notes = [], [n for n in it.notes if not n.startswith("original")]
        for t in it.tickets:
            legs = fresh[t.spec_key]
            same = [l for l in legs if l.signature == t.signature]
            if same:
                tickets.append(min(same, key=lambda l: l.price))
                continue
            ok = [l for l in legs if (l.stops == 0 if t.stops == 0 else direct_ok(l))]
            if not ok:
                break
            alt = min(ok, key=lambda l: l.price)
            notes.append(f"original {t.origin}→{t.dest} flight no longer listed; using {alt.depart:%d %b %H:%M}")
            tickets.append(alt)
        else:
            out.append(replace(it, tickets=tickets, notes=notes))
            continue
        log(f"  {route_label(it)}: no flights left on that search, dropped")
    out.sort(key=lambda i: (i.ground_unknown, i.total))
    return out


# ---------- trend analysis ----------

def slope(points: list[tuple[date, float]]) -> float | None:
    """Least-squares GBP/day over the points given."""
    if len(points) < 2:
        return None
    xs = [(d - points[0][0]).days for d, _ in points]
    ys = [p for _, p in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return None if den == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def itinerary_trend(it_id: str, rows: list[dict]) -> dict:
    pts = sorted((date.fromisoformat(r["checked_on"]), float(r["total"])) for r in rows if r["id"] == it_id)
    if not pts:
        return {}
    recent = [p for p in pts if (pts[-1][0] - p[0]).days <= 14]
    return {"first_seen": pts[0][0], "first": pts[0][1], "low": min(p for _, p in pts),
            "high": max(p for _, p in pts), "now": pts[-1][1], "runs": len(pts), "slope": slope(recent)}


def ticket_insight(spec_key: str, rows: list[dict]) -> dict:
    """Latest Google 60-day history summary for this search, plus our own observations."""
    mine = sorted((date.fromisoformat(r["checked_on"]), float(r["min_price"]))
                  for r in rows if r["spec"] == spec_key and r["min_price"])
    latest = next((r for r in reversed(rows) if r["spec"] == spec_key and r["g_points"]), None)
    out = {"ours": mine}
    if latest:
        out["google"] = {k[2:]: float(latest[k]) for k in OBS_FIELDS if k.startswith("g_") and k != "g_from"}
        out["google"]["from"] = latest["g_from"]
    return out


def verdict(now: float, google: dict | None, our_slope: float | None) -> str:
    """Plain-English read on whether this price is worth taking now."""
    level = ""
    trend = google.get("slope_14d") if google else our_slope
    if google and google.get("median"):
        ratio = now / google["median"]
        level = "high" if ratio > 1.15 else "low" if ratio < 0.92 else "typical"
    rising = trend is not None and trend > 1
    falling = trend is not None and trend < -1
    if level == "low":
        return "Low vs its 60-day history: good time to book"
    if level == "high" and falling:
        return "High but falling: watch for a few days"
    if level == "high":
        return "High vs its history and not falling: holiday fares rarely recover, consider booking"
    if rising:
        return "Rising: likely to keep climbing as Christmas nears"
    if falling:
        return "Falling: worth watching"
    if level == "typical":
        return "Typical price, flat"
    return "Not enough history yet"
