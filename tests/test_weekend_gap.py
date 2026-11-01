"""weekend_gap.py: pairing, executable gap, sessions, storage and the /gap report."""
import calendar

import pytest

import weekend_gap as wg

SUN_EVE = calendar.timegm((2026, 11, 1, 20, 0, 0)) * 1000        # Sunday 20:00 UTC
TUE_US = calendar.timegm((2026, 11, 3, 15, 0, 0)) * 1000         # Tuesday 15:00 UTC


def hl_ctx(mid, half=0.0005, mark=None):
    return {"midPx": str(mid), "impactPxs": [str(mid * (1 - half)), str(mid * (1 + half))],
            "markPx": str(mark or mid), "oraclePx": str(mid), "funding": "0.00001", "dayNtlVlm": "5000000"}


def snapshot(gold_aster=3900.0, qqq=700.0):
    hl = {"GOLD": hl_ctx(3880.0), "NVDA": hl_ctx(200.0), "SPX": hl_ctx(7600.0), "XYZ100": hl_ctx(28000.0),
          "ONLYHL": hl_ctx(10.0)}
    prem = {"XAUUSDT": {"markPrice": str(gold_aster), "indexPrice": "3880", "lastFundingRate": "0.0001"},
            "NVDAUSDT": {"markPrice": "200.1", "indexPrice": "200", "lastFundingRate": "0"},
            "SPXUSDT": {"markPrice": "0.35", "indexPrice": "0.35", "lastFundingRate": "0"},     # a meme coin
            "QQQUSDT": {"markPrice": str(qqq), "indexPrice": str(qqq), "lastFundingRate": "0"}}
    book = {s: {"bidPrice": str(float(p["markPrice"]) * 0.9999), "askPrice": str(float(p["markPrice"]) * 1.0001),
                "bidQty": "1", "askQty": "1"} for s, p in prem.items()}
    return hl, book, prem


def test_pairs_match_by_name_alias_and_price():
    hl, book, prem = snapshot()
    assert wg.pairs(hl, prem) == {"GOLD": ("XAUUSDT", False), "NVDA": ("NVDAUSDT", False),
                                  "XYZ100": ("QQQUSDT", True)}       # SPX dropped: price 0.35 vs 7600


def test_gap_direction_and_size():
    g, d = wg.gap_bp({"hl_bid": 99.9, "hl_ask": 100.1, "as_bid": 100.5, "as_ask": 100.6})
    assert d == "A>H" and g == pytest.approx((100.5 - 100.1) / 100.275 * 1e4)
    g, d = wg.gap_bp({"hl_bid": 99.9, "hl_ask": 100.1, "as_bid": 99.95, "as_ask": 100.05})
    assert g < 0                                                    # overlapping books: nothing to trade
    assert wg.gap_bp({"hl_bid": "", "hl_ask": 1, "as_bid": 1, "as_ask": 1}) == (None, None)


def test_sessions():
    assert wg.session(SUN_EVE) == "weekend"
    assert wg.session(SUN_EVE + 3 * 3_600_000) == "off-hours"           # Sun 23:00: futures open
    assert wg.session(TUE_US) == "US hours"
    assert wg.session(calendar.timegm((2026, 10, 30, 21, 30, 0)) * 1000) == "weekend"   # Fri 21:30


def test_log_rotate_and_report(tmp_path):
    hl, book, prem = snapshot()
    pm = wg.pairs(hl, prem)
    for i in range(10):                                         # Sunday: Aster gold 45bp rich at the tops
        wg.append(tmp_path, wg.rows_for(SUN_EVE + i * 60_000, pm, hl, book, prem))
    hl2, book2, prem2 = snapshot(gold_aster=3880.0)
    for i in range(10):                                         # Tuesday US hours: books overlap
        wg.append(tmp_path, wg.rows_for(TUE_US + i * 60_000, pm, hl2, book2, prem2))
    wg.gzip_old(tmp_path, TUE_US)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["gap_20261101.csv.gz", "gap_20261103.csv"]
    rows = wg.load(tmp_path, TUE_US + 3_600_000)
    assert len(rows) == 60
    G = wg.gaps(rows)
    xyz = [g for g in G if g[1] == "XYZ100"]
    assert len(xyz) == 20 and max(abs(g[3]) for g in xyz) < 10          # ratio pair: constant ratio, no gap
    rep = wg.report(tmp_path, TUE_US + 3_600_000)
    assert "weekend</b> (30 pair-minutes)" in rep and "US hours</b> (30 pair-minutes)" in rep
    assert "• GOLD: median +45bp" in rep and "Aster rich 100%" in rep
    assert "off-hours</b>: no samples" in rep


def test_report_empty(tmp_path):
    assert "no samples yet" in wg.report(tmp_path, SUN_EVE)
