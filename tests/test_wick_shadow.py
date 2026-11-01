"""wick_shadow.py: ladder placement, the trigger pull, the latency model, exits, capacity and the report."""
import csv

import pytest

import wick_shadow as wsh

T0 = 1_793_000_000_000
LAG, OW = 400, 115


def feed(eng, prints, lag=LAG, coin="X"):
    """prints: (ms after T0, px, sz). Each arrives `lag` ms later; on_tick runs with every print."""
    for dt, px, sz in prints:
        t = T0 + dt
        eng.on_lag(t + lag, lag)
        eng.on_trade(t, coin, px, sz, t + lag)
        eng.on_tick(t + lag)


def calm(until_ms, px=1.0, every=100):
    return [(i, px, 100) for i in range(0, until_ms, every)]


def fills(d):
    try:
        with open(d / "fills.csv", newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


@pytest.fixture
def eng(tmp_path):
    return wsh.Engine(tmp_path, vlm={"X": 20e6}, ow_ms=OW)


def test_ladder_goes_up_after_5s_of_history(eng):
    feed(eng, calm(4900))
    assert not eng.coins["X"].up
    feed(eng, calm(6000)[50:])
    c = eng.coins["X"]
    assert c.up and sorted(round(o["px"], 4) for o in c.orders) == [0.97, 0.98, 1.02, 1.03]


def test_sweep_fills_both_levels_and_takes_profit_halfway_back(eng, tmp_path):
    feed(eng, calm(10_000))
    # one sweep within 150ms: trigger at 0.99, through 0.98 and 0.97, then the snap back
    feed(eng, [(10_000, 0.99, 50), (10_050, 0.975, 2000), (10_150, 0.965, 3000),
               (12_000, 0.985, 100), (13_000, 0.9951, 100)])
    f = {r["level_bp"]: r for r in fills(tmp_path)}
    assert set(f) == {"200", "300"}
    assert f["200"]["reason"] == "tp" and float(f["200"]["exit"]) == pytest.approx(0.99)
    assert float(f["200"]["net_bp"]) == pytest.approx(0.99 / 0.98 * 1e4 - 1e4 - 3, abs=0.01)
    assert f["300"]["reason"] == "tp" and float(f["300"]["exit"]) == pytest.approx(0.985)
    assert f["200"]["tf_ms"] == "50" and f["200"]["vlm"] == "$10-50M"
    assert not eng.coins["X"].up                       # ladder stays off after the trigger
    assert eng.cnt["triggers"] == 1 and eng.cnt["cancels"] >= 1


def test_slow_move_is_pulled_before_it_reaches_the_ladder(eng, tmp_path):
    feed(eng, calm(10_000))
    # -1bp every 10ms: the 80bp trigger fires ~0.8s in, the 200bp level is only reached ~2s in
    feed(eng, [(10_000 + 10 * i, 1.0 - 0.0001 * i, 100) for i in range(1, 400)])
    assert fills(tmp_path) == [] and eng.cnt["fills"] == 0
    assert eng.cnt["triggers"] == 1


def test_prints_before_the_cancel_lands_still_fill(eng, tmp_path):
    feed(eng, calm(10_000))
    # trigger at t; the cancel lands at t + LAG + OW; a print through 0.98 just before that fills,
    # one just after does not reach the (already dead) 0.97 level
    feed(eng, [(10_000, 0.99, 50), (10_000 + LAG + OW - 1, 0.979, 100), (10_000 + LAG + OW + 1, 0.969, 100),
               (12_000, 0.995, 100)])                     # the take-profit is itself only live 515ms after the fill
    assert [r["level_bp"] for r in fills(tmp_path)] == ["200"]


def test_feed_lag_pulls_everything(eng, tmp_path):
    feed(eng, calm(10_000))
    feed(eng, calm(10_200)[100:], lag=2000)           # feed falls 2s behind
    assert eng.lag_bad and not eng.coins["X"].up and eng.cnt["lag_pulls"] == 1
    feed(eng, [(12_000, 0.99, 50), (12_050, 0.975, 2000), (13_000, 0.995, 100)], lag=2000)
    assert fills(tmp_path) == []


def test_no_reversion_exits_at_market_after_60s(eng, tmp_path):
    feed(eng, calm(10_000))
    feed(eng, [(10_000, 0.99, 50), (10_050, 0.975, 2000)] + [(10_100 + 500 * i, 0.975, 10) for i in range(130)])
    (f,) = fills(tmp_path)
    assert f["reason"] == "time" and f["level_bp"] == "200"
    assert float(f["net_bp"]) == pytest.approx((0.975 / 0.98 - 1) * 1e4 - 1.5 - 6.5, abs=0.01)
    assert int(f["exit_ms"]) >= int(f["fill_ms"]) + wsh.MAX_HOLD_MS


def test_size_capped_at_share_of_volume_through_the_level(eng, tmp_path):
    feed(eng, calm(10_000))
    feed(eng, [(10_000, 0.99, 50), (10_050, 0.979, 500), (10_500, 0.979, 250), (13_000, 0.9951, 100)])
    (f,) = fills(tmp_path)
    assert float(f["size_usd"]) == pytest.approx(0.2 * 0.979 * 750, abs=0.01)      # $147 < $300


def test_order_is_not_live_until_it_reaches_the_exchange(eng, tmp_path):
    feed(eng, calm(5200))                              # first print 5s old at 5100: sent at 5100+LAG, live OW later
    c = eng.coins["X"]
    live = c.orders[0]["live"]
    assert live == pytest.approx(T0 + 5100 + LAG + OW)
    feed(eng, [(int(live - T0) - 10, 0.975, 1000), (int(live - T0) + 2000, 0.995, 100)])
    assert fills(tmp_path) == []


def test_disconnect_closes_positions_at_market_on_reconnect(eng, tmp_path):
    feed(eng, calm(10_000))
    feed(eng, [(10_000, 0.99, 50), (10_050, 0.975, 2000)])
    eng.on_disconnect(T0 + 11_000)
    assert eng.cnt["disconnects"] == 1
    eng.on_connect()
    feed(eng, [(20_000, 0.982, 10)])
    (f,) = fills(tmp_path)
    assert f["reason"] == "disconnect" and float(f["exit"]) == pytest.approx(0.982)


def test_report(eng, tmp_path):
    assert "no fills yet" in wsh.report(tmp_path)
    feed(eng, calm(10_000))
    feed(eng, [(10_000, 0.99, 50), (10_050, 0.975, 2000), (10_150, 0.965, 3000), (13_000, 0.9951, 100)])
    eng.flush_lag()
    rep = wsh.report(tmp_path, now_ms=T0 + 20_000)
    assert "2 fills" in rep and "+200bp $10-50M: 1 fills" in rep and "+300bp $10-50M: 1 fills" in rep
    assert "exits: tp 2" in rep and "triggers 1 (1 with the ladder up)" in rep and "feed lag: median 400ms" in rep


def test_repeg_threshold_and_request_count(tmp_path):
    e = wsh.Engine(tmp_path, vlm={"X": 20e6}, ow_ms=OW, repeg_bp=50)
    feed(e, calm(6000))
    assert e.cnt["orders"] == 4 and e.cnt["modifies"] == 0         # 4 orders = 4 requests
    feed(e, [(6000 + 100 * i, 1.003, 100) for i in range(80)])     # 30bp drift: below the threshold
    assert e.cnt["modifies"] == 0
    feed(e, [(14_000 + 100 * i, 1.006, 100) for i in range(80)])   # 60bp up: only the sells (near side) move
    c = e.coins["X"]
    assert e.cnt["modifies"] == 2 and c.ref == {1: 1.0, -1: pytest.approx(1.006)}
    assert sorted(round(o["px"], 5) for o in c.orders if o["dead"] == wsh.INF) == [0.97, 0.98, 1.02612, 1.03618]
    feed(e, [(22_000 + 100 * i, 1.0105, 100) for i in range(80)])  # 105bp from the buys' ref: far side refreshed too
    assert e.cnt["modifies"] == 4 and c.ref == {1: pytest.approx(1.0105), -1: pytest.approx(1.006)}
