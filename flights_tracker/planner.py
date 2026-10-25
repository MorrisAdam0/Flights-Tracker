"""Brute-forces every way of getting there and back for a trip and ranks them by total cost.

Ways out of London to a gateway airport:
  direct   London -> gateway, one ticket, <=1 stop, <=16h
  via hub  London -> European hub, then hub -> gateway, two nonstop tickets, 0-2 nights in the hub
Ways back are the mirror image. Every way out is paired with every way back (open-jaw allowed),
plus single return tickets: London <-> gateway, and hub <-> gateway with hops to/from London.
In-country cost is in -> must-visit city -> out, by bus/train where close, else a live-priced flight.
"""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from itertools import product

from . import config as C
from .config import Trip
from .search import Leg, Searcher

HUB_RETURN_PAIRS = 6  # cheapest hub<->gateway pairs to also price as hub return tickets


@dataclass
class Route:
    """One direction: one or two tickets, plus any nights in a hub."""

    kind: str  # "direct" or "via XXX"
    tickets: list[Leg]
    nights: int = 0

    @property
    def cost(self) -> int:
        return sum(t.price for t in self.tickets) + self.nights * C.STOPOVER_NIGHT_GBP


@dataclass
class Itinerary:
    trip: str
    strategy: str
    fly_in: str
    fly_out: str
    leave_london: date
    leave_country: date
    tickets: list[Leg]
    nights_in_hubs: int = 0
    ground: list[tuple[str, int | None]] = field(default_factory=list)  # (description, GBP)
    notes: list[str] = field(default_factory=list)

    @property
    def flights_cost(self) -> int:
        return sum(t.price for t in self.tickets)

    @property
    def stopover_cost(self) -> int:
        return self.nights_in_hubs * C.STOPOVER_NIGHT_GBP

    @property
    def ground_cost(self) -> int:
        return sum(c or 0 for _, c in self.ground)

    @property
    def ground_unknown(self) -> bool:
        return any(c is None for _, c in self.ground)

    @property
    def total(self) -> int:
        return self.flights_cost + self.stopover_cost + self.ground_cost

    @property
    def id(self) -> str:
        sig = ";".join(t.signature for t in self.tickets) + f"|{self.fly_in}|{self.fly_out}"
        return hashlib.sha1(sig.encode()).hexdigest()[:10]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["leave_london"] = self.leave_london.isoformat()
        d["leave_country"] = self.leave_country.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Itinerary":
        return cls(**{
            **d,
            "leave_london": date.fromisoformat(d["leave_london"]),
            "leave_country": date.fromisoformat(d["leave_country"]),
            "tickets": [Leg.from_dict(t) for t in d["tickets"]],
            "ground": [tuple(g) for g in d["ground"]],
        })


# ---------- rules ----------

def direct_ok(leg: Leg) -> bool:
    return leg.stops <= C.MAX_STOPS and leg.journey_minutes <= C.MAX_JOURNEY_MINUTES


def home_in_time(leg: Leg) -> bool:
    return leg.arrive.date() <= C.LATEST_ARRIVAL_HOME


def connection_nights(first: Leg, second: Leg) -> int | None:
    """Nights spent between two separate tickets at the same hub, or None if it doesn't work."""
    gap = second.depart - first.arrive
    nights = (second.depart.date() - first.arrive.date()).days
    if gap < timedelta(minutes=C.MIN_SELF_TRANSFER_MINUTES) or nights > C.MAX_STOPOVER_DAYS:
        return None
    return nights


# ---------- search orchestration ----------

def _run(searcher: Searcher, specs: list[tuple], workers: int) -> dict[tuple, list[Leg]]:
    specs = list(dict.fromkeys(specs))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda s: searcher.one_way(*s) if len(s) == 5 else searcher.round_trip(*s), specs)
        return dict(zip(specs, results))


def _flat(results: dict) -> list[Leg]:
    return [l for legs in results.values() for l in legs]


def _keep_best(best: dict, key, route: Route) -> None:
    if key not in best or route.cost < best[key].cost:
        best[key] = route


def _hub_long_haul(trip: Trip, searcher: Searcher, workers: int, thorough: bool, log) -> dict[tuple, list[Leg]]:
    """Nonstops hub -> gateway (outbound days) and gateway -> hub (return days).

    Most hub/gateway pairs have no nonstop at all. Unless `thorough`, the return days (only 3)
    are searched in full but the outbound days only on a few dates, and a pair counts as served
    if either direction turned up a flight (or a search failed, so we can't tell). Together that
    covers six dates on different weekdays before ruling a route out. Served pairs then get every
    outbound date."""
    hubs = list(trip.hubs)
    out_days = C.days(C.OUTBOUND_DATES[0], C.OUTBOUND_DATES[-1] + timedelta(C.MAX_STOPOVER_DAYS))
    pairs = list(product(hubs, trip.hub_gateways))
    probe_out = out_days if thorough else out_days[::3]
    specs = [(h, g, d, 0, None) for (h, g), d in product(pairs, probe_out)]
    specs += [(g, h, d, 0, None) for (h, g), d in product(pairs, C.RETURN_DATES)]
    log(f"[{trip.name}] step 1b: {len(specs)} hub long-haul searches" + ("" if thorough else " (finding which routes exist)"))
    results = _run(searcher, specs, workers)
    if thorough:
        return results

    served = set()
    for spec, legs in results.items():
        if legs or spec_key(spec) not in searcher.observations:  # found flights, or the search failed
            a, b = spec[:2]
            served.add((a, b) if a in trip.hubs else (b, a))
    fill = [(h, g, d, 0, None) for (h, g) in pairs if (h, g) in served for d in out_days if d not in probe_out]
    log(f"[{trip.name}] step 1c: {len(served)} hub routes have nonstops; {len(fill)} more searches for their other dates")
    return {**results, **_run(searcher, fill, workers)}


def spec_key(spec: tuple) -> str:
    from .search import Spec

    origin, dest, day, stops, minutes = spec
    return Spec(origin, dest, day, None, stops, minutes).key


def plan(trip: Trip, searcher: Searcher, workers: int = 2, thorough: bool = False, log=print) -> list[Itinerary]:
    gws, hubs = list(trip.gateways), list(trip.hubs)
    home_days = C.days(C.RETURN_DATES[0], C.LATEST_ARRIVAL_HOME)

    # 1. Direct one-ways, and the long-haul nonstops from each hub.
    specs = (
        [(o, g, d, C.MAX_STOPS, C.MAX_JOURNEY_MINUTES) for o, g, d in product(C.LONDON_LONG_HAUL, gws, C.OUTBOUND_DATES)]
        + [(g, o, d, C.MAX_STOPS, C.MAX_JOURNEY_MINUTES) for g, o, d in product(gws, C.LONDON_LONG_HAUL, C.RETURN_DATES)]
    )
    log(f"[{trip.name}] step 1a: {len(specs)} direct-from-London searches")
    legs = _flat(_run(searcher, specs, workers)) + _flat(_hub_long_haul(trip, searcher, workers, thorough, log))
    direct_out = [l for l in legs if l.origin in C.LONDON_LONG_HAUL and direct_ok(l)]
    direct_back = [l for l in legs if l.dest in C.LONDON_LONG_HAUL and direct_ok(l) and home_in_time(l)]
    lh_out = [l for l in legs if l.origin in hubs and l.stops == 0]
    lh_back = [l for l in legs if l.dest in hubs and l.stops == 0]

    # 2. Short nonstop hops between London and the hubs, only on dates that can connect:
    #    up to MAX_STOPOVER_DAYS before a long-haul departure, or after a long-haul arrival.
    span = range(C.MAX_STOPOVER_DAYS + 1)
    out_dates = {(l.origin, l.depart.date() - timedelta(k)) for l in lh_out for k in span}
    back_dates = {(l.dest, l.arrive.date() + timedelta(k)) for l in lh_back for k in span}
    # Hub return tickets only give the return date, so allow hops home up to 3 days after it.
    back_dates |= {(l.dest, l.depart.date() + timedelta(k)) for l in lh_back for k in range(C.MAX_STOPOVER_DAYS + 2)}
    specs = sorted(
        [(o, h, d, 0, None) for o in C.LONDON_ALL for h, d in out_dates if d in C.OUTBOUND_DATES]
        + [(h, o, d, 0, None) for o in C.LONDON_ALL for h, d in back_dates if d in home_days],
        key=str,
    )
    log(f"[{trip.name}] step 2: {len(specs)} London<->hub searches "
        f"({len({h for h, _ in out_dates})} hubs out, {len({h for h, _ in back_dates})} back)")
    legs = _flat(_run(searcher, specs, workers))
    to_hub = [l for l in legs if l.origin in C.LONDON_ALL and l.stops == 0]
    from_hub = [l for l in legs if l.dest in C.LONDON_ALL and l.stops == 0 and home_in_time(l)]

    # 3. Ways out, best per (gateway, day leaving London, kind); ways back per (gateway, day leaving).
    outs: dict[tuple, Route] = {}
    for l in direct_out:
        _keep_best(outs, (l.dest, l.depart.date(), "direct"), Route("direct", [l]))
    for lh in lh_out:
        for short in to_hub:
            if short.dest == lh.origin and (n := connection_nights(short, lh)) is not None:
                _keep_best(outs, (lh.dest, short.depart.date(), "hub"), Route(f"via {lh.origin}", [short, lh], n))
    backs: dict[tuple, Route] = {}
    for l in direct_back:
        _keep_best(backs, (l.origin, l.depart.date(), "direct"), Route("direct", [l]))
    for lh in lh_back:
        for short in from_hub:
            if short.origin == lh.dest and (n := connection_nights(lh, short)) is not None:
                _keep_best(backs, (lh.origin, lh.depart.date(), "hub"), Route(f"via {lh.dest}", [lh, short], n))

    # 4. In-country hops to and from the must-visit city.
    must = trip.must_visit
    hop_specs = [(g, must, C.HOP_TO_MUST_VISIT, 1, None) for g in gws if g != must and frozenset({g, must}) not in trip.overland]
    hop_specs += [(must, g, C.HOP_FROM_MUST_VISIT, 1, None) for g in gws if g != must and frozenset({g, must}) not in trip.overland]
    log(f"[{trip.name}] step 3: {len(hop_specs)} in-country flight searches")
    hop_prices = {(s[0], s[1]): min((l.price for l in legs), default=None)
                  for s, legs in _run(searcher, hop_specs, workers).items()}

    def hop(a: str, b: str) -> tuple[str, int | None]:
        if frozenset({a, b}) in trip.overland:
            return f"bus/train/ferry {a}→{b} (est.)", trip.overland[frozenset({a, b})]
        price = hop_prices.get((a, b))
        when = C.HOP_TO_MUST_VISIT if b == must else C.HOP_FROM_MUST_VISIT
        return (f"flight {a}→{b} {when:%d %b}" if price else f"{a}→{b} not priced"), price

    def ground(g_in: str, g_out: str) -> list[tuple[str, int | None]]:
        if g_in == g_out == must:
            return []
        if must in (g_in, g_out):
            return [hop(g_in, g_out)] if g_in != g_out else []
        return [hop(g_in, must), hop(must, g_out)]

    # 5. Pair every way out with every way back.
    itins: list[Itinerary] = []
    for (g_in, d_out, _), out in outs.items():
        for (g_out, d_back, _), back in backs.items():
            strategy = "Direct one-ways" if out.kind == back.kind == "direct" else f"Out {out.kind}, back {back.kind}"
            itins.append(Itinerary(trip.name, strategy, g_in, g_out, d_out, d_back, out.tickets + back.tickets,
                                   out.nights + back.nights, ground(g_in, g_out)))

    # 6. Return tickets from London, to gateways that have direct flights both ways.
    both_ways = sorted({l.dest for l in direct_out} & {l.origin for l in direct_back})
    rt_specs = [(o, g, d1, d2, C.MAX_STOPS, C.MAX_JOURNEY_MINUTES)
                for o, g, d1, d2 in product(C.LONDON_LONG_HAUL, both_ways, C.OUTBOUND_DATES, C.RETURN_DATES)]

    # ...and return tickets from the hubs on the cheapest hub<->gateway pairs.
    cheapest = {}
    for l in lh_out + lh_back:
        pair = (l.origin, l.dest) if l.origin in hubs else (l.dest, l.origin)
        cheapest.setdefault(pair, {}).setdefault(l.origin in hubs, l.price)
        cheapest[pair][l.origin in hubs] = min(cheapest[pair][l.origin in hubs], l.price)
    pairs = sorted((p for p, v in cheapest.items() if len(v) == 2), key=lambda p: sum(cheapest[p].values()))
    for h, g in pairs[:HUB_RETURN_PAIRS]:
        out_days = sorted({l.depart.date() for l in lh_out if (l.origin, l.dest) == (h, g)})
        back_days = sorted({l.depart.date() for l in lh_back if (l.origin, l.dest) == (g, h)})
        rt_specs += [(h, g, d1, d2, 0, None) for d1, d2 in product(out_days, back_days)]
    log(f"[{trip.name}] step 4: {len(rt_specs)} return-ticket searches")
    for spec, legs in _run(searcher, rt_specs, workers).items():
        origin, g, d1, d2 = spec[:4]
        legs = [l for l in legs if (direct_ok(l) if origin in C.LONDON_LONG_HAUL else l.stops == 0)]
        if not legs:
            continue
        rt = min(legs, key=lambda l: l.price)
        if origin in C.LONDON_LONG_HAUL:
            itins.append(Itinerary(trip.name, "London return", g, g, d1, d2, [rt], 0, ground(g, g)))
            continue
        # Hub return: add the cheapest hop London->hub before and hub->London after.
        hop_out = [(connection_nights(h_, rt), h_) for h_ in to_hub if h_.dest == origin]
        hop_out = [(n, h_) for n, h_ in hop_out if n is not None]
        # Only the return's date is known, not its times: assume it lands at the hub the
        # next day (overnight flight) and needs a hop home leaving after 18:00 or later.
        lands = datetime(d2.year, d2.month, d2.day, 12) + timedelta(days=1)
        hop_back = [((h_.depart.date() - lands.date()).days, h_) for h_ in from_hub
                    if h_.origin == origin and h_.depart >= lands + timedelta(hours=6)]
        hop_back = [(n, h_) for n, h_ in hop_back if n <= C.MAX_STOPOVER_DAYS]
        if not hop_out or not hop_back:
            continue
        n1, a = min(hop_out, key=lambda x: x[1].price + x[0] * C.STOPOVER_NIGHT_GBP)
        n2, b = min(hop_back, key=lambda x: x[1].price + x[0] * C.STOPOVER_NIGHT_GBP)
        it = Itinerary(trip.name, f"{origin} return + hops", g, g, a.depart.date(), d2, [a, rt, b], n1 + n2, ground(g, g))
        it.notes.append(f"check the {g}→{origin} return lands in time for the {b.depart:%d %b %H:%M} flight home")
        itins.append(it)

    # Unpriced in-country legs would look misleadingly cheap, so they go last.
    itins.sort(key=lambda i: (i.ground_unknown, i.total))
    log(f"[{trip.name}] {len(itins)} itineraries; cheapest £{itins[0].total if itins else '-'}")
    return itins
