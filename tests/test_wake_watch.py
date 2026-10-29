"""wake_watch.py: the AIN-pattern filter, history loading, and the 24h/72h follow-up."""
import csv
import gzip
import time

import pytest

import wake_watch as ww

H = ww.HOUR_MS
NOW = 1_793_000_000_000 - (1_793_000_000_000 % H)
COLS = ["ts_ms", "symbol", "mark", "funding_apr", "mexc_ask_5pct", "max_day_gain_30d", "age_days", "score",
        "perp_volume_usd"]


def ain_like(sym="AINUSDT", fund_now=0.6, ask_now=3.0, mark_now=0.040, gain=1.5, age=120, vol=200_000, end=NOW,
             hours=40, fund_base=0.11):
    rows = []
    for i in range(hours, -1, -1):
        rows.append({"ts_ms": end - i * H, "symbol": sym, "mark": mark_now if i == 0 else 0.038,
                     "funding_apr": fund_now if i <= 3 else fund_base, "mexc_ask_5pct": ask_now * 1000 if i == 0 else 5000,
                     "max_day_gain_30d": gain, "age_days": age, "score": 5, "perp_volume_usd": vol})
    return rows


def write_log(d, rows):
    by_day = {}
    for r in rows:
        by_day.setdefault(time.strftime("%Y%m%d", time.gmtime(r["ts_ms"] / 1000)), []).append(r)
    for i, (day, rs) in enumerate(sorted(by_day.items())):
        path = d / (f"scam_log_{day}.csv" + (".gz" if i == 0 else ""))      # older day rotated
        with (gzip.open if str(path).endswith(".gz") else open)(path, "wt", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLS)
            w.writeheader()
            w.writerows(rs)


@pytest.fixture
def logs(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    d.mkdir()
    monkeypatch.setattr(ww, "LOG_DIR", d)
    return d


def test_ain_pattern_matches_and_variants_do_not(logs):
    write_log(logs, ain_like() + ain_like("MOVEDUSDT", mark_now=0.050) + ain_like("YOUNGUSDT", age=20)
              + ain_like("NOSPIKEUSDT", gain=0.2) + ain_like("THICKUSDT", ask_now=5.0) + ain_like("COLDUSDT", fund_now=0.2))
    hist = ww.load_history(NOW)
    assert len(hist) == 6
    assert {s for s, rows in hist.items() if ww.matches(ww.features(rows, NOW))} == {"AINUSDT"}


def test_not_enough_history_never_matches(logs):
    write_log(logs, ain_like()[-6:])
    assert not ww.matches(ww.features(ww.load_history(NOW)["AINUSDT"], NOW))


def test_alert_once_then_24h_and_72h_followups(logs, tmp_path):
    w = ww.Watch(tmp_path / "state")
    write_log(logs, ain_like())
    hist = ww.load_history(NOW)
    msgs = w.step(NOW, hist)
    assert len(msgs) == 1 and "AINUSDT" in msgs[0] and "WATCH ONLY" in msgs[0]

    def at(t, mark):    # a later sweep: same history, plus the new mark
        h = {k: list(v) for k, v in hist.items()}
        h["AINUSDT"].append({**h["AINUSDT"][-1], "ts": t, "mark": mark})
        return w.step(t, h)

    assert at(NOW + H, 0.12) == []                                   # no repeat alert
    m24 = at(NOW + 24 * H, 0.10)
    assert len(m24) == 1 and "after 24h: +150%" in m24[0] and "max +200%" in m24[0]
    m72 = at(NOW + 72 * H, 0.06)
    assert any("after 72h: +50%" in m for m in m72)
    assert any("1 completed: 1 reached +30%, 1 reached +100%" in m for m in m72)
    assert float(w.results()[0]["max_72h"]) == pytest.approx(2.0)
    assert "AINUSDT" not in w.load_state()["active"]


def test_rearm_window(logs, tmp_path):
    w = ww.Watch(tmp_path / "state")
    write_log(logs, ain_like())
    hist = ww.load_history(NOW)
    w.step(NOW, hist)
    st = w.load_state()
    st["active"].clear()
    w.save_state(st)
    assert w.step(NOW + 2 * H, hist) == []


def test_variants_tagged_and_tallied_separately(logs, tmp_path):
    w = ww.Watch(tmp_path / "state")
    # +2.6% over 6h is "flat", +5.3% is not; $20k volume fails "liquid"; the base rule takes all three
    write_log(logs, ain_like(mark_now=0.039) + ain_like("RUNUPUSDT") + ain_like("DEADUSDT", mark_now=0.039, vol=20_000))
    hist = ww.load_history(NOW)
    msgs = w.step(NOW, hist)
    assert len(msgs) == 3
    act = w.load_state()["active"]
    assert (act["AINUSDT"]["flat"], act["AINUSDT"]["liquid"]) == (True, True)
    assert (act["RUNUPUSDT"]["flat"], act["RUNUPUSDT"]["liquid"]) == (False, True)
    assert (act["DEADUSDT"]["flat"], act["DEADUSDT"]["liquid"]) == (True, False)
    for s in act:
        hist[s].append({**hist[s][-1], "ts": NOW + 72 * H, "mark": 0.076 if s == "AINUSDT" else 0.038})
    w.step(NOW + 72 * H, hist)
    out = w.stats_line()
    assert out.splitlines()[0].startswith("3 completed: 1 reached +30%")
    assert "flat (±5%/6h): 2 completed: 1 reached +30%" in out
    assert "liquid (≥$50k/day): 2 completed: 1 reached +30%" in out
    assert "flat+liquid: 1 completed: 1 reached +30%, 0 reached +100%" in out


def test_carry_rule_paper_trades(logs, tmp_path):
    w = ww.Watch(tmp_path / "state")
    w.fund_hi30 = lambda sym, now: {"UPUSDT": 0.5, "DOWNUSDT": 2.0}.get(sym)
    calm = dict(fund_now=0.2, fund_base=0.2, ask_now=5.0, mark_now=0.038, hours=80)
    write_log(logs, ain_like("UPUSDT", **calm) + ain_like("DOWNUSDT", **calm) + ain_like("FLATUSDT", **calm)
              + ain_like("HOTUSDT", **{**calm, "fund_now": 0.5, "fund_base": 0.5})        # 50%/yr: out of band
              + ain_like("NEGUSDT", **{**calm, "fund_now": -0.1, "fund_base": -0.1}))
    hist = ww.load_history(NOW)
    assert w.step(NOW, hist) == []                                       # silent: no Telegram messages
    assert set(w.load_state()["carry"]) == {"UPUSDT", "DOWNUSDT", "FLATUSDT"}

    def at(t, marks):
        h = {k: list(v) for k, v in hist.items()}
        for s, m in marks.items():
            h[s].append({**h[s][-1], "ts": t, "mark": m})
        return w.step(t, h)

    at(NOW + 5 * H, {"UPUSDT": 0.080, "DOWNUSDT": 0.027, "FLATUSDT": 0.039})   # +110% -> tp, -29% -> sl
    at(NOW + 72 * H, {"FLATUSDT": 0.0399})                                     # time exit +5%
    res = {r["symbol"]: r for r in w.carry_results()}
    assert (res["UPUSDT"]["reason"], float(res["UPUSDT"]["ret"])) == ("tp", 1.0)
    assert (res["DOWNUSDT"]["reason"], float(res["DOWNUSDT"]["ret"])) == ("sl", -0.25)
    assert res["FLATUSDT"]["reason"] == "time" and float(res["FLATUSDT"]["ret"]) == pytest.approx(0.05)
    assert "3 closed: $+80 total" in w.carry_line()
    assert (res["UPUSDT"]["nosqueeze"], res["DOWNUSDT"]["nosqueeze"], res["FLATUSDT"]["nosqueeze"]) == ("1", "0", "0")
    assert "no 30d squeeze (backtest +2.3-4.4%): 1 closed: $+100" in w.carry_line()
    assert "funding +13-16% (backtest +3.4-4.4%): none closed" in w.carry_line()


def test_carry_skips_coins_falling_over_24h(logs, tmp_path):
    w = ww.Watch(tmp_path / "state")
    w.fund_hi30 = lambda sym, now: None
    rows = ain_like("FALLUSDT", fund_now=0.15, fund_base=0.15, ask_now=5.0, mark_now=0.038, hours=80)
    for r in rows:                                   # 0.040 a day ago -> 0.038 now: -5% over 24h, flat over 6h
        if r["ts_ms"] <= NOW - 20 * H:
            r["mark"] = 0.040
    write_log(logs, rows + ain_like("OKUSDT", fund_now=0.15, fund_base=0.15, ask_now=5.0, mark_now=0.038, hours=80))
    w.step(NOW, ww.load_history(NOW))
    st = w.load_state()["carry"]
    assert set(st) == {"OKUSDT"} and ww.carry_tags(st["OKUSDT"]) == {"low": True, "nosqueeze": False}


def test_results_header_upgrade(tmp_path):
    p = tmp_path / "r.csv"
    p.write_text("a,b\n1,2\n")
    ww._append_csv(p, ["a", "b", "c"], {"a": 3, "b": 4, "c": 5})
    ww._append_csv(p, ["a", "b", "c"], {"a": 6, "b": 7, "c": 8})
    assert p.read_text().splitlines() == ["a,b,c", "1,2,", "3,4,5", "6,7,8"]
