from datetime import date, datetime, timedelta

from flights_tracker import report
from flights_tracker.config import MEXICO
from flights_tracker.planner import plan
from flights_tracker.search import Leg, Segment

PRICES = {"CUN": 400, "MEX": 450, "MID": 380, "OAX": 600}


def leg(origin, dest, depart, hours, price, via=None, layover_h=2):
    if via is None:
        arr = depart + timedelta(hours=hours)
        segs = [Segment(origin, dest, depart.isoformat(), arr.isoformat(), hours * 60)]
    else:
        half = hours // 2
        a1 = depart + timedelta(hours=half)
        d2 = a1 + timedelta(hours=layover_h)
        a2 = d2 + timedelta(hours=hours - half)
        segs = [Segment(origin, via, depart.isoformat(), a1.isoformat(), half * 60),
                Segment(via, dest, d2.isoformat(), a2.isoformat(), (hours - half) * 60)]
    return Leg(origin, dest, price, ["XX"], segs)


class FakeSearcher:
    failures: list = []
    queries = 0

    def one_way(self, origin, dest, day, max_stops=1):
        t = datetime(day.year, day.month, day.day, 10)
        if origin in ("LHR", "LGW"):
            if dest not in PRICES or origin == "LGW":
                return []
            return [
                leg(origin, dest, t, 11, PRICES[dest]),
                leg(origin, dest, t, 15, PRICES[dest] - 200, via="JFK", layover_h=3),  # 18h: too long
            ]
        if dest in ("LHR", "LGW"):
            if origin not in PRICES or dest == "LGW":
                return []
            late = datetime(day.year, day.month, day.day, 20)  # overnight, lands next day
            return [leg(origin, dest, late, 10, PRICES[origin] + 50)]
        return [leg(origin, dest, t, 2, 70)]  # domestic hop

    def round_trip(self, origin, dest, d1, d2):
        return []


def test_rules_and_ranking():
    combos, legs = plan(MEXICO, FakeSearcher(), log=lambda *_: None)
    assert legs
    for c in combos:
        assert c.outbound.journey_minutes <= 16 * 60
        assert c.inbound.arrive.date() <= date(2027, 1, 10)
        # 10 Jan overnight departures land on the 11th, so are excluded.
        assert c.inbound.depart.date() <= date(2027, 1, 9)

    best = combos[0]
    # Cheapest out is MID (380); cheapest back is MID (430). Same airport, no ground cost.
    assert (best.fly_in, best.fly_out, best.total) == ("MID", "MID", 810)
    # CUN<->MID is on the overland list; CUN->OAX is a priced domestic flight.
    cun_mid = next(c for c in combos if (c.fly_in, c.fly_out) == ("CUN", "MID"))
    assert cun_mid.ground_cost == 25 and cun_mid.total == 400 + 430 + 25
    cun_oax = next(c for c in combos if (c.fly_in, c.fly_out) == ("CUN", "OAX"))
    assert cun_oax.ground_cost == 70


def test_report_renders():
    combos, _ = plan(MEXICO, FakeSearcher(), log=lambda *_: None)
    md = report.markdown("Mexico", combos)
    assert "£810" in md and "google.com/travel/flights" in md
