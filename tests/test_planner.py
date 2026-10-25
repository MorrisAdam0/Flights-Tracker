from datetime import date, datetime, timedelta

import pytest

from flights_tracker import history, planner, report
from flights_tracker.config import MEXICO
from flights_tracker.search import Leg, Observation, Searcher, Segment, Spec, find_price_series, summarise_series


def flight(spec, dep: datetime, hours: float, price: int, via: str | None = None, layover_h: float = 2):
    """A leg with naive local times: arrival = dep + hours (good enough for tests)."""
    if via is None:
        segs = [Segment(spec.origin, spec.dest, dep.isoformat(), (dep + timedelta(hours=hours)).isoformat(), int(hours * 60))]
    else:
        a1 = dep + timedelta(hours=hours / 2)
        d2 = a1 + timedelta(hours=layover_h)
        segs = [Segment(spec.origin, via, dep.isoformat(), a1.isoformat(), int(hours * 30)),
                Segment(via, spec.dest, d2.isoformat(), (d2 + timedelta(hours=hours / 2)).isoformat(), int(hours * 30))]
    return Leg(spec.origin, spec.dest, price, ["XX"], segs, spec.key)


def at(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm)


class FakeSearcher(Searcher):
    """A tiny made-up world: London<->Cancún/Mexico City direct, and Madrid as a cheap hub."""

    def __init__(self, mad_cun_return=450, price_bump=0):
        super().__init__(use_cache=False, delay=0)
        self.mad_cun_return = mad_cun_return
        self.bump = price_bump
        self.seen: list[Spec] = []

    def search(self, spec: Spec) -> list[Leg]:
        self.seen.append(spec)
        legs = [l for l in self._world(spec) if spec.max_stops is None or l.stops <= spec.max_stops]
        for l in legs:
            l.price += self.bump
        self.observations[spec.key] = Observation(spec.key, min((l.price for l in legs), default=None), len(legs))
        return legs

    def _world(self, s: Spec) -> list[Leg]:
        d, route = s.day, (s.origin, s.dest)
        if s.back:
            if route == ("LHR", "CUN"):
                return [flight(s, at(d, 10), 11, 900)]
            if route == ("MAD", "CUN") and (d, s.back) == (date(2026, 12, 28), date(2027, 1, 8)):
                return [flight(s, at(d, 10), 4, self.mad_cun_return)]
            return []
        if route == ("LHR", "CUN"):
            return [flight(s, at(d, 10), 11, 500),
                    flight(s, at(d, 7), 16, 300, via="JFK", layover_h=3)]  # 19h door to door: too long
        if route == ("LHR", "MEX"):
            return [flight(s, at(d, 11), 12, 650)]
        if route == ("CUN", "LHR"):
            return [flight(s, at(d, 20), 10, 520)]  # overnight; leaving 9 Jan lands 10 Jan
        if route == ("MEX", "LHR"):
            return [flight(s, at(d, 19), 11, 600)]
        if route == ("MAD", "CUN") and d == date(2026, 12, 28):
            return [flight(s, at(d, 10), 4, 250)]
        if route == ("CUN", "MAD") and d == date(2027, 1, 8):
            return [flight(s, at(d, 18), 16, 260)]  # lands 9 Jan 10:00
        if route == ("STN", "MAD") and d == date(2026, 12, 27):
            return [flight(s, at(d, 18), 3.5, 30)]  # 1 night in Madrid
        if route == ("LGW", "MAD") and d == date(2026, 12, 28):
            return [flight(s, at(d, 6), 3.5, 20)]  # lands 09:30 for a 10:00 flight: too tight
        if route == ("LTN", "MAD") and d == date(2026, 12, 28):
            return [flight(s, at(d, 4), 2.5, 45)]  # lands 06:30: 3.5h to connect, fine
        if route == ("MAD", "LGW") and d == date(2027, 1, 9):
            return [flight(s, at(d, 15), 2.5, 35)]
        if route == ("MAD", "LGW") and d == date(2027, 1, 10):
            return [flight(s, at(d, 8), 2.5, 50)]
        if route == ("CUN", "MEX"):
            return [flight(s, at(d, 9), 2.5, 60)]
        if route == ("MEX", "CUN"):
            return [flight(s, at(d, 9), 2.5, 55)]
        return []


def run(**kw):
    return planner.plan(MEXICO, FakeSearcher(**kw), log=lambda *_: None)


def test_cheapest_uses_madrid_both_ways_and_visits_mexico_city():
    best = run()[0]
    # Out: LTN->MAD 45 + MAD->CUN 250. Back: CUN->MAD 260 + MAD->LGW 35. CUN->MEX 60, MEX->CUN 55.
    assert [f"{t.origin}{t.dest}" for t in best.tickets] == ["LTNMAD", "MADCUN", "CUNMAD", "MADLGW"]
    assert best.total == 45 + 250 + 260 + 35 + 60 + 55
    assert best.nights_in_hubs == 0
    assert [c for _, c in best.ground] == [60, 55]


def test_rules_are_enforced():
    itins = run()
    for it in itins:
        assert it.leave_london <= date(2026, 12, 31)
        assert date(2027, 1, 7) <= it.leave_country <= date(2027, 1, 9)
        for t in it.tickets:
            assert "JFK" not in t.via  # the 19h one-stop is never used
            if t.origin == "LGW" and t.dest == "MAD":
                pytest.fail("a 30-minute self-transfer was accepted")
        if it.tickets[-1].dest in ("LHR", "LGW") and not it.tickets[-1].round_trip:
            assert it.tickets[-1].arrive.date() <= date(2027, 1, 10)


def test_stopover_night_is_costed():
    itins = run()
    # Leaving London on the 27th means the STN flight and a night in Madrid.
    it = next(i for i in itins if i.leave_london == date(2026, 12, 27) and i.tickets[0].origin == "STN")
    assert it.nights_in_hubs == 1 and it.stopover_cost == 70


def test_open_jaw_and_must_visit():
    itins = run()
    # Into Cancún, home from Mexico City direct: one in-country flight CUN->MEX.
    it = next(i for i in itins if i.fly_in == "CUN" and i.fly_out == "MEX" and i.strategy == "Direct one-ways")
    assert it.total == 500 + 600 + 60
    # Mexico City both ways: no in-country cost.
    it = next(i for i in itins if i.fly_in == i.fly_out == "MEX")
    assert it.ground == []


def test_hub_return_ticket_wins_when_cheaper():
    best = run(mad_cun_return=380)[0]
    # LTN->MAD 45, MAD<->CUN 380, then MAD->LGW on 10 Jan (the 9 Jan 15:00 is too early) 50 + 1 night.
    assert best.strategy == "MAD return + hops"
    assert best.total == 45 + 380 + 50 + 70 + 60 + 55


def test_london_returns_are_priced():
    itins = run()
    assert any(i.strategy == "London return" and i.total == 900 + 60 + 55 for i in itins)


def test_reprice_tracks_same_flights(tmp_path, monkeypatch):
    itins = run()[:5]
    again = history.reprice(itins, FakeSearcher(price_bump=10), log=lambda *_: None)
    assert [i.id for i in again] == [i.id for i in itins]
    assert all(b.total == a.total + 10 * len(a.tickets) for a, b in zip(itins, again))


def test_history_and_report(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(history, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(report, "RESULTS_DIR", tmp_path)
    s = FakeSearcher()
    itins = planner.plan(MEXICO, s, log=lambda *_: None)
    history.record_observations(s)
    history.record_itineraries(itins[:10])
    history.record_itineraries(itins[:10])  # same day again: replaced, not duplicated
    assert len(history.load_csv("itineraries.csv")) == 10
    md = report.trip_section("Mexico", itins, 20, tracking=False)
    assert "£705" in md and "Cheapest by strategy" in md and "google.com/travel/flights" in md
    report.write_itineraries_csv("Mexico", itins)
    assert (tmp_path / "mexico_itineraries.csv").exists()


def test_price_series_found_anywhere_in_payload():
    day = 86_400_000
    series = [[1_760_000_000_000 + i * day, 400 + i * 2] for i in range(30)]
    payload = [None, [[1, 2], ["x", [None, series]]], [[1_760_000_000_000, 5]]]
    found = find_price_series(payload)
    assert len(found) == 30
    s = summarise_series(found)
    assert s["min"] == 400 and s["max"] == 458 and s["slope_14d"] == pytest.approx(2.0)


def test_verdicts():
    g = {"median": 500, "slope_14d": 0}
    assert history.verdict(420, g, None).startswith("Low")
    assert history.verdict(650, {**g, "slope_14d": 3}, None).startswith("High vs")
    assert history.verdict(650, {**g, "slope_14d": -3}, None).startswith("High but falling")
    assert history.verdict(500, None, 4.0).startswith("Rising")
