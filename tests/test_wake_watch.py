"""wake_watch.py: the AIN-pattern filter, history loading, and the 24h/72h follow-up."""
import csv
import gzip
import time

import pytest

import wake_watch as ww

H = ww.HOUR_MS
NOW = 1_793_000_000_000 - (1_793_000_000_000 % H)
COLS = ["ts_ms", "symbol", "mark", "funding_apr", "mexc_ask_5pct", "max_day_gain_30d", "age_days", "score"]


def ain_like(sym="AINUSDT", fund_now=0.6, ask_now=3.0, mark_now=0.040, gain=1.5, age=120, end=NOW):
    rows = []
    for i in range(40, -1, -1):
        rows.append({"ts_ms": end - i * H, "symbol": sym, "mark": mark_now if i == 0 else 0.038,
                     "funding_apr": fund_now if i <= 3 else 0.11, "mexc_ask_5pct": ask_now * 1000 if i == 0 else 5000,
                     "max_day_gain_30d": gain, "age_days": age, "score": 5})
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
