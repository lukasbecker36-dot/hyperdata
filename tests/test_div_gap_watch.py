import json
from datetime import datetime, timezone

import pytest

import div_gap_watch as dg


def test_gap_picks_the_tradable_side():
    g, side = dg.gap(h_bid=99.9, h_ask=100.0, a_bid=102.0, a_ask=102.1)
    assert g == pytest.approx(0.02) and side.startswith("short Aster")
    g, side = dg.gap(h_bid=102.0, h_ask=102.1, a_bid=99.9, a_ask=100.0)
    assert g == pytest.approx(0.02) and side.startswith("short HL")


def test_windows_open_two_days_before_and_one_after():
    cal = {"names": {"skhx": [{"from": "2026-11-27", "to": "2026-11-27", "pct": 1.9}]}}
    assert "SKHX" in dg.windows(cal, "2026-11-25") and "SKHX" in dg.windows(cal, "2026-11-28")
    assert dg.windows(cal, "2026-11-24") == {} and dg.windows(cal, "2026-11-29") == {}


def test_step_needs_persistence_then_grows_and_closes():
    st = {}
    assert [dg.step(st, "X", 0.012, True) for _ in range(3)] == [None, None, "open"]
    assert dg.step(st, "X", 0.015, True) is None
    assert dg.step(st, "X", 0.018, True) == "grow"
    assert dg.step(st, "X", 0.002, True) == "close"
    st = {}
    assert all(dg.step(st, "Y", 0.015, False) is None for _ in range(40))       # 1.5% isn't enough off-calendar
    assert [dg.step(st, "Y", 0.025, False) for _ in range(30)][-1] == "open"


def test_depth_counts_only_the_band():
    assert dg.depth_usd([["100", "10"], ["99.6", "10"], ["99.4", "10"]], 100.0, 0.005, "bid") == pytest.approx(1996)


class Fake:
    def __init__(self, a_bid, depth_qty=100.0):
        self.a_bid, self.depth_qty = a_bid, depth_qty

    def __call__(self, url, body=None):
        if body and body["type"] == "meta":
            return {"universe": [{"name": "xyz:SKHX"}, {"name": "xyz:IBM"}]}
        if body and body["type"] == "metaAndAssetCtxs":
            return [{"universe": [{"name": "xyz:SKHX"}, {"name": "xyz:IBM"}]},
                    [{"impactPxs": ["772.0", "773.0"], "funding": "-0.0001"}, {"impactPxs": ["300", "300.1"]}]]
        if "exchangeInfo" in url:
            return {"symbols": [{"symbol": "SKHYNIXUSDT", "status": "TRADING", "underlyingSubType": ["STOCK"]},
                                {"symbol": "IBMUSDT", "status": "TRADING", "underlyingSubType": ["STOCK"]},
                                {"symbol": "BBUSDT", "status": "TRADING", "underlyingSubType": []}]}
        if "bookTicker" in url:
            return [{"symbol": "SKHYNIXUSDT", "bidPrice": str(self.a_bid), "askPrice": str(self.a_bid + 0.3)},
                    {"symbol": "IBMUSDT", "bidPrice": "299.9", "askPrice": "300.2"}]
        if "depth" in url:
            return {"bids": [[str(self.a_bid), str(self.depth_qty)]], "asks": []}
        raise AssertionError(url)


def run_checks(tmp_path, monkeypatch, fake, n, cal=True):
    ex = tmp_path / "exdiv.json"
    ex.write_text(json.dumps({"names": {"SKHX": [{"from": "2026-11-27", "to": "2026-11-27", "pct": 1.9}]} if cal else {}}))
    monkeypatch.setattr(dg, "EXDIV", ex)
    sent = []
    w = dg.Watch(tmp_path / "dg", notify=sent.append, fetch=fake)
    now = datetime(2026, 11, 26, 14, tzinfo=timezone.utc).timestamp()
    for i in range(n):
        w.check(now + 60 * i)
    return w, sent


def test_gap_about_the_dividend_is_fair_and_not_alerted(tmp_path, monkeypatch):
    w, sent = run_checks(tmp_path, monkeypatch, Fake(a_bid=789.0), 5)        # +2.07% vs a 1.9% dividend
    assert sent == []


def test_excess_subtracts_the_dividend_only_when_aster_is_rich():
    g, side = dg.excess(99.9, 100.0, 103.0, 103.1, 0.019)
    assert g == pytest.approx(0.011) and side.startswith("short Aster")
    g, side = dg.excess(101.5, 101.6, 99.9, 100.0, 0.019)
    assert g == pytest.approx(0.015) and side.startswith("short HL")


def test_alerts_skhynix_beyond_its_dividend(tmp_path, monkeypatch):
    w, sent = run_checks(tmp_path, monkeypatch, Fake(a_bid=797.0), 3)        # +3.1%: 1.2% beyond the dividend
    assert len(sent) == 1 and "div gap SKHX" in sent[0] and "short Aster" in sent[0] and "ex-div 2026-11-27" in sent[0]
    assert "IBM" not in sent[0]


def test_thin_aster_book_is_not_alerted(tmp_path, monkeypatch):
    w, sent = run_checks(tmp_path, monkeypatch, Fake(a_bid=797.0, depth_qty=1.0), 5)
    assert sent == []


def test_off_calendar_gap_needs_two_percent_for_half_an_hour(tmp_path, monkeypatch):
    w, sent = run_checks(tmp_path, monkeypatch, Fake(a_bid=789.0), 29, cal=False)      # +2.07%
    assert sent == []
    w.check(datetime(2026, 11, 26, 15, tzinfo=timezone.utc).timestamp())
    assert len(sent) == 1 and "no ex-date in the calendar" in sent[0]


def test_only_stock_listings_are_paired(tmp_path, monkeypatch):
    w, sent = run_checks(tmp_path, monkeypatch, Fake(a_bid=789.0), 1)
    w.get = lambda url, body=None: ({"universe": [{"name": "xyz:SKHX"}, {"name": "xyz:BB"}]} if body
                                    else Fake(1.0)(url))
    w.refresh_pairs()
    assert w.pairs == {"SKHX": "SKHYNIXUSDT"}                     # Aster's BBUSDT is a crypto coin
