import json

import pytest

from flights_tracker import fetch, search
from flights_tracker.fetch import PageError, classify
from flights_tracker.search import SearchAborted, Searcher, Spec, parse_page
from datetime import date


def segment(frm, to, dep, arr, minutes, day):
    s = [None] * 22
    s[3], s[4], s[5], s[6] = frm, f"{frm} airport", f"{to} airport", to
    s[8], s[10], s[11], s[17] = dep, arr, minutes, "Boeing 787"
    s[20] = s[21] = list(day)
    return s


def results_page(price=512, class_attr=True):
    flight = [None] * 23
    flight[0], flight[1] = "BA", ["British Airways"]
    flight[2] = [segment("LHR", "CUN", [10, 30], [15], 690, (2026, 12, 27))]
    flight[22] = [None] * 7 + [600000, 650000]
    payload = [None] * 8
    payload[3] = [[[flight, [[None, price]]]]]
    payload[7] = [None, [[["OW", "oneworld"]], [["BA", "British Airways"]]]]
    cls = ' class="ds:1"' if class_attr else ""
    return (f"<html><body><script{cls} nonce=\"x\">AF_initDataCallback({{key: 'ds:1', hash: '1', "
            f"data:{json.dumps(payload)}, sideChannel: {{}}}});</script></body></html>")


CONSENT = "<html><title>Before you continue to Google</title><form action='https://consent.google.com/save'></form></html>"
SORRY = "<html>Our systems have detected unusual traffic from your computer network.</html>"


def test_parses_results_page():
    legs, _ = parse_page(results_page(), "k")
    assert len(legs) == 1
    l = legs[0]
    assert (l.origin, l.dest, l.price, l.depart.hour, l.depart.minute, l.journey_minutes) == ("LHR", "CUN", 512, 10, 30, 690)


def test_finds_results_without_class_attribute():
    legs, _ = parse_page(results_page(class_attr=False), "k")
    assert legs[0].price == 512


def test_classifies_failures():
    assert classify(results_page()) is None
    assert classify(CONSENT) == "consent"
    assert classify("<html></html>", url="https://consent.google.com/ml?continue=...") == "consent"
    assert classify(SORRY) == "captcha"
    assert classify("<html></html>", status=429) == "rate_limited"
    assert classify("<html>nothing</html>") == "no_payload"
    with pytest.raises(PageError) as e:
        parse_page(CONSENT, "k")
    assert e.value.kind == "consent"


class FakeFetcher:
    def __init__(self, html, name="http"):
        self.html, self.name, self.calls = html, name, 0

    def fetch(self, params):
        self.calls += 1
        return self.html, "https://www.google.com/travel/flights", 200


@pytest.fixture
def searcher(tmp_path, monkeypatch):
    monkeypatch.setattr(search, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(fetch, "DEBUG_DIR", tmp_path / "debug")
    monkeypatch.setattr(search, "browser_available", lambda: False)
    return Searcher(use_cache=False, delay=0)


def spec(n=0):
    return Spec("LHR", "CUN", date(2026, 12, 27 + n % 5), max_stops=n // 5)


def test_success_records_observation(searcher):
    searcher.fetcher = FakeFetcher(results_page())
    legs = searcher.search(spec())
    assert legs[0].price == 512 and searcher.observations[spec().key].min_price == 512


def test_stops_early_when_everything_fails(searcher):
    searcher.fetcher = FakeFetcher(CONSENT)
    with pytest.raises(SearchAborted) as e:
        for n in range(20):
            searcher.search(spec(n))
    assert "consent" in str(e.value) and "playwright" in str(e.value)
    assert searcher.fetcher.calls == Searcher.ABORT_IF_FIRST_FAIL  # consent pages aren't retried
    assert len(searcher.debug_pages) == 3 and searcher.debug_pages[0].exists()


def test_switches_to_browser_on_consent(searcher, monkeypatch):
    browser = FakeFetcher(results_page(600), name="browser")
    monkeypatch.setattr(search, "browser_available", lambda: True)
    monkeypatch.setattr(search, "BrowserFetcher", lambda: browser)
    searcher.fetcher = FakeFetcher(CONSENT)
    assert searcher.search(spec())[0].price == 600
    assert searcher.fetcher is browser


def test_no_switch_when_mode_is_http(tmp_path, monkeypatch):
    monkeypatch.setattr(search, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(fetch, "DEBUG_DIR", tmp_path)
    monkeypatch.setattr(search, "browser_available", lambda: True)
    s = Searcher(use_cache=False, delay=0, fetcher="http")
    s.fetcher = FakeFetcher(CONSENT)
    assert s.search(spec()) == [] and s.failure_kinds == {"consent": 1}
