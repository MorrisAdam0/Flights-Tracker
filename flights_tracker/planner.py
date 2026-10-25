"""Builds every in/out combination for a trip and ranks them by total cost."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from itertools import product

from .config import LATEST_ARRIVAL_HOME, LONDON, OUTBOUND_DATES, RETURN_DATES, Trip
from .search import Leg, Searcher

DOMESTIC_DATE = date(2027, 1, 4)  # mid-trip; the hop date is yours to move
DOMESTIC_PAIRS_TO_PRICE = 40


@dataclass
class Combo:
    trip: str
    outbound: Leg
    inbound: Leg | None  # None for a round-trip fare (return leg chosen on Google)
    ground_cost: int = 0
    ground_note: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def flights_cost(self) -> int:
        return self.outbound.price + (self.inbound.price if self.inbound else 0)

    @property
    def total(self) -> int:
        return self.flights_cost + self.ground_cost

    @property
    def fly_in(self) -> str:
        return self.outbound.dest

    @property
    def fly_out(self) -> str:
        return self.inbound.origin if self.inbound else self.outbound.dest

    @property
    def nights(self) -> int | None:
        if not self.inbound:
            return None
        return (self.inbound.depart.date() - self.outbound.arrive.date()).days


def _cheapest(legs: list[Leg]) -> Leg | None:
    return min(legs, key=lambda l: (l.price, l.journey_minutes), default=None)


def valid_outbound(legs: list[Leg]) -> list[Leg]:
    return [l for l in legs if l.ok()]


def valid_inbound(legs: list[Leg]) -> list[Leg]:
    return [l for l in legs if l.ok() and l.arrive.date() <= LATEST_ARRIVAL_HOME]


def combine(
    trip: Trip,
    outbound: dict[tuple[str, date], Leg],
    inbound: dict[tuple[str, date], Leg],
    domestic: dict[tuple[str, str], Leg | None],
) -> list[Combo]:
    """Every (fly-in gateway, out date) x (fly-out gateway, return date) pairing."""
    combos = []
    for (g_in, _), out_leg in outbound.items():
        for (g_out, _), in_leg in inbound.items():
            c = Combo(trip.name, out_leg, in_leg)
            if g_in != g_out:
                pair = frozenset({g_in, g_out})
                if pair in trip.overland:
                    c.ground_cost = trip.overland[pair]
                    c.ground_note = f"bus/train/ferry {g_in}→{g_out} (est.)"
                elif (hop := domestic.get((g_in, g_out))) is not None:
                    c.ground_cost = hop.price
                    c.ground_note = f"domestic flight {g_in}→{g_out} on {DOMESTIC_DATE:%d %b}"
                else:
                    c.ground_note = f"{g_in}→{g_out} not priced"
                    c.notes.append("internal transfer cost unknown")
            combos.append(c)
    return combos


def plan(trip: Trip, searcher: Searcher, workers: int = 3, log=print) -> tuple[list[Combo], list[Leg]]:
    gateways = list(trip.gateways)

    def run(jobs):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda j: j[0](*j[1:]), jobs))

    # 1. Every one-way out of London and every one-way back.
    out_jobs = [(searcher.one_way, o, g, d) for o, g, d in product(LONDON, gateways, OUTBOUND_DATES)]
    in_jobs = [(searcher.one_way, g, o, d) for g, o, d in product(gateways, LONDON, RETURN_DATES)]
    log(f"[{trip.name}] {len(out_jobs)} outbound + {len(in_jobs)} return one-way searches")
    out_results = run(out_jobs)
    in_results = run(in_jobs)

    all_legs: list[Leg] = []
    best_out: dict[tuple[str, date], Leg] = {}
    best_in: dict[tuple[str, date], Leg] = {}
    for (_, _, g, d), legs in zip(out_jobs, out_results):
        all_legs += legs
        if (leg := _cheapest(valid_outbound(legs))) and (
            (g, d) not in best_out or leg.price < best_out[(g, d)].price
        ):
            best_out[(g, d)] = leg
    for (_, g, _, d), legs in zip(in_jobs, in_results):
        all_legs += legs
        if (leg := _cheapest(valid_inbound(legs))) and (
            (g, d) not in best_in or leg.price < best_in[(g, d)].price
        ):
            best_in[(g, d)] = leg

    # 2. Price the domestic hop for the open-jaw pairs that look cheapest on flights alone.
    rough = sorted(combine(trip, best_out, best_in, {}), key=lambda c: c.flights_cost)
    pairs: list[tuple[str, str]] = []
    for c in rough:
        p = (c.fly_in, c.fly_out)
        if c.fly_in != c.fly_out and frozenset(p) not in trip.overland and p not in pairs:
            pairs.append(p)
        if len(pairs) >= DOMESTIC_PAIRS_TO_PRICE:
            break
    log(f"[{trip.name}] {len(pairs)} domestic hop searches")
    dom_results = run([(searcher.one_way, a, b, DOMESTIC_DATE) for a, b in pairs])
    domestic = {p: _cheapest([l for l in legs if l.price > 0]) for p, legs in zip(pairs, dom_results)}

    combos = combine(trip, best_out, best_in, domestic)

    # 3. Round-trip fares to the main hubs: sometimes cheaper than two one-ways.
    rt_jobs = [
        (searcher.round_trip, o, h, d1, d2)
        for o, h, d1, d2 in product(LONDON, trip.round_trip_hubs, OUTBOUND_DATES, RETURN_DATES)
    ]
    log(f"[{trip.name}] {len(rt_jobs)} round-trip searches")
    for job, legs in zip(rt_jobs, run(rt_jobs)):
        if leg := _cheapest(valid_outbound(legs)):
            c = Combo(trip.name, leg, None)
            c.notes.append(f"round trip, back {job[4]:%a %d %b}: check the return leg is ≤16h and lands by 10 Jan")
            combos.append(c)

    # Combos with an unpriced internal transfer would look misleadingly cheap.
    combos.sort(key=lambda c: ("internal transfer cost unknown" in c.notes, c.total))
    return combos, all_legs
