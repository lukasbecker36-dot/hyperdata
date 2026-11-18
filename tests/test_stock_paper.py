"""stock_paper.py: New York clock, portfolio construction, accounting, and a simulated day of ticks."""
import calendar
import csv

import pytest

import stock_paper as sp

H = 3600


def utc(*a):
    return calendar.timegm(a + (0,) * (6 - len(a)))


def test_new_york_clock_follows_us_dst():
    assert sp.ny_offset(utc(2026, 10, 30, 15)) == -4          # EDT
    assert sp.ny_offset(utc(2026, 11, 3, 15)) == -5           # EST after 1 Nov
    assert sp.ny_time("2026-10-30", 11) == utc(2026, 10, 30, 15)
    assert sp.ny_time("2026-11-03", 11) == utc(2026, 11, 3, 16)
    assert sp.ny_time("2026-11-02", 16) == utc(2026, 11, 2, 21)
    assert sp.ny_day(utc(2026, 11, 4, 2)) == "2026-11-03"     # 21:00 NY on the 3rd


def test_trading_days():
    assert sp.is_full_trading_day("2026-11-03")
    assert not sp.is_full_trading_day("2026-11-26")            # Thanksgiving
    assert not sp.is_full_trading_day("2026-11-27")            # half day
    assert not sp.is_full_trading_day("2026-11-07")            # Saturday
    assert sp.prev_trading_day("2026-11-02") == "2026-10-30"   # Monday -> Friday
    assert sp.prev_trading_day("2026-11-27") == "2026-11-25"   # skips Thanksgiving


def test_long_short_inverse_vol_and_minimum():
    sig = {c: i for i, c in enumerate("ABCDEFGHIJ")}
    vol = {c: 0.01 for c in sig} | {"J": 0.02}
    w = sp.long_short(sig, vol, 0.2, min_n=2)
    assert set(w) == {"A", "B", "I", "J"}
    assert w["I"] == pytest.approx(1000 * 2 / 3) and w["J"] == pytest.approx(1000 / 3)
    assert w["A"] == pytest.approx(-500) and sum(v for v in w.values() if v > 0) == pytest.approx(1000)
    assert sp.long_short(sig, vol, 0.2, min_n=3) == {}         # 2 a side < 3


def test_cap_spreads_the_excess_and_falls_back_to_equal_weights():
    w = sp.capped_weights({"A": 10.0, **{c: 1.0 for c in "BCDEFGHIJKLM"}}, 0.10)
    assert w["A"] == pytest.approx(0.10) and sum(w.values()) == pytest.approx(1.0)
    assert max(w.values()) <= 0.10 + 1e-9 and w["B"] == pytest.approx(0.90 / 12)
    assert sp.capped_weights({"A": 5.0, "B": 1.0}, 0.10) == {"A": 0.5, "B": 0.5}     # 2 names can't all be <= 10%
    sig = {c: i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmn")}
    vol = {c: 0.01 for c in sig} | {"n": 0.0001}                                      # one very calm name
    w = sp.long_short(sig, vol, 0.3, cap=0.10)
    assert w["n"] == pytest.approx(100.0) and sum(v for v in w.values() if v > 0) == pytest.approx(1000.0)


def test_carry_target_averages_cohorts():
    t = sp.carry_target([{"A": 300.0}, {"A": 300.0, "B": -300.0}, {"B": -300.0}, {"C": 300.0}])
    assert t == pytest.approx({"A": 100.0, "B": -200.0, "C": 100.0})   # last three cohorts only


def test_intraday_signal_is_relative_to_median():
    s = sp.intraday_signal({"A": 110.0, "B": 100.0, "C": 90.0, "D": 50.0}, {"A": 100.0, "B": 100.0, "C": 100.0})
    assert s["B"] == pytest.approx(0.0)                        # B is the median mover
    assert s["A"] == pytest.approx(sp.math.log(1.1)) and s["C"] == pytest.approx(sp.math.log(0.9))
    assert "D" not in s                                        # no previous close


def test_mark_rebalance_funding_and_costs():
    b, trades = sp.new_book(), []
    snap = {"A": {"mid": 100.0, "funding": 0.0001, "vol": 1e6}, "B": {"mid": 50.0, "funding": -0.0002, "vol": 1e6}}
    sp.mark(b, snap, 0)
    sp.rebalance(b, {"A": 1000.0, "B": -1000.0}, snap, 0, "x", "in", trades)
    assert b["cost"] == pytest.approx(2000 * sp.COST) and len(trades) == 2
    snap2 = {"A": {"mid": 110.0, "funding": 0.0001, "vol": 1e6}, "B": {"mid": 45.0, "funding": -0.0002, "vol": 1e6}}
    sp.mark(b, snap2, 2 * H)
    assert b["price"] == pytest.approx(100 + 100)                       # +10% long, -10% under the short
    # long A (now $1,100) pays 1bp/h x 2h; short B (now -$900) pays 2bp/h x 2h because funding is negative
    assert b["funding"] == pytest.approx(-1100 * 0.0001 * 2 - 900 * 0.0002 * 2)
    sp.rebalance(b, {}, snap2, 2 * H, "x", "out", trades)
    assert b["pos"] == {} and b["cost"] == pytest.approx((2000 + 2000) * sp.COST)


def test_isolated_liquidation_loses_the_margin(tmp_path):
    b, trades = sp.new_book(), []
    q = lambda px: {"A": {"mid": px, "funding": 0.0, "vol": 1e6}, "B": {"mid": px, "funding": 0.0, "vol": 1e6}}
    sp.mark(b, q(100.0), 0, lev=4)
    sp.rebalance(b, {"A": 100.0, "B": -100.0}, q(100.0), 0, "carry", "in", trades)
    assert sp.liq_price(b["pos"]["A"], 4, 0.05) == pytest.approx(80.0)
    assert sp.liq_price(b["pos"]["B"], 4, 0.05) == pytest.approx(120.0)
    sp.mark(b, q(90.0), H, lev=4, name="carry", trades=trades)          # -10%: both still alive
    assert set(b["pos"]) == {"A", "B"} and b["price"] == pytest.approx(-10 + 10)
    sp.mark(b, {"A": {"mid": 79.0, "funding": 0.0, "vol": 1e6}}, 2 * H, lev=4, name="carry", trades=trades)
    assert "A" not in b["pos"] and b["liqs"] == 1 and b["liq_usd"] == pytest.approx(-25.0)
    assert b["price"] == pytest.approx(-25.0 + 10.0)                      # A lost its $25 margin; B still +$10
    assert trades[-1]["reason"] == "liquidated 4x"


def test_rebalance_tracks_entry_for_liquidation():
    b, tr = sp.new_book(), []
    s = lambda px: {"A": {"mid": px, "funding": 0.0, "vol": 1e6}}
    sp.rebalance(b, {"A": 100.0}, s(100.0), 0, "x", "in", tr)
    sp.mark(b, s(110.0), H)
    sp.rebalance(b, {"A": 220.0}, s(110.0), H, "x", "add", tr)           # 1 unit at 100 + 1 unit at 110
    assert b["pos"]["A"]["entry"] == pytest.approx(105.0)
    sp.rebalance(b, {"A": 110.0}, s(110.0), H, "x", "trim", tr)
    assert b["pos"]["A"]["entry"] == pytest.approx(105.0)
    sp.rebalance(b, {"A": -110.0}, s(110.0), H, "x", "flip", tr)
    assert b["pos"]["A"]["entry"] == pytest.approx(110.0)


def test_tick_runs_both_books_on_schedule(tmp_path):
    st = sp.load_state(tmp_path)
    snap = {c: {"mid": 100.0, "funding": 0.0, "vol": 1e6} for c in "ABCDEF"}
    calls = []

    def carry(s, now, log):
        calls.append(("carry", now))
        return {"A": 1000.0, "B": -1000.0}, {}

    def intra(s, now, day, log):
        calls.append(("intra", day))
        return {"C": 1000.0, "D": -1000.0}, {}

    b = {"carry": carry, "intraday": intra}
    t0 = utc(2026, 11, 3, 0)                                   # Tue 00:00 UTC
    for h in range(0, 24):
        if h == 17:
            snap = {**snap, "C": {"mid": 101.0, "funding": 0.0, "vol": 1e6}}    # C up 1% while held
        sp.tick(st, snap, t0 + h * H, tmp_path, log=lambda m: None, builders=b)
    assert calls == [("carry", t0), ("intra", "2026-11-03")]     # carry once at 00:00, intraday once at 16:00 UTC
    assert st["intra_open"] is False and st["intraday"]["pos"] == {}     # out at 19:00 UTC (14:00 NY)
    assert st["intraday"]["price"] == pytest.approx(10.0)
    assert st["carry"]["pos"]["A"]["usd"] == pytest.approx(1000 / 3)    # first cohort of three
    with open(tmp_path / "trades.csv", newline="") as f:
        tr = list(csv.DictReader(f))
    got = [(int(r["ts"]), r["coin"], r["reason"]) for r in tr if r["book"] == "intraday"]
    assert sorted(got) == [(t0 + 16 * H, "C", "entry 11:00 NY"), (t0 + 16 * H, "D", "entry 11:00 NY"),
                           (t0 + 19 * H, "C", "exit 14:00 NY"), (t0 + 19 * H, "D", "exit 14:00 NY")]
    sp.save_state(tmp_path, st)
    rep = sp.report(tmp_path, now=t0 + 24 * H)
    # +$10 on C, minus 2.5bp on $2,000 in and $2,010 out
    assert "US intraday momentum" in rep and "P&amp;L <b>$+9.00</b>" in rep


def test_report_is_valid_telegram_html(tmp_path):
    """Telegram rejects the whole message on a stray '<' or '&' (it parses HTML) -- /stocks then says nothing."""
    import re
    rep = sp.report(tmp_path)
    assert "n/a (under 5 days)" in rep
    assert not re.search(r"<(?!/?(b|i)>)", rep)
    assert not re.search(r"&(?!(amp|lt|gt);)", rep)


def test_no_intraday_on_holiday(tmp_path):
    st = sp.load_state(tmp_path)
    snap = {"C": {"mid": 100.0, "funding": 0.0, "vol": 1e6}}
    calls = []
    b = {"carry": lambda s, n, l: ({}, {}), "intraday": lambda s, n, d, l: calls.append(d) or ({}, {})}
    t0 = utc(2026, 11, 26, 0)                                  # Thanksgiving
    for h in range(24):
        sp.tick(st, snap, t0 + h * H, tmp_path, log=lambda m: None, builders=b)
    assert calls == []
