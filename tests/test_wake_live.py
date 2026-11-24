import json
from decimal import Decimal

import pytest

import wake_live as wl

H = wl.HOUR_MS
T0 = 1_800_000_000_000


class Clock:
    def __init__(self, ms):
        self.ms = ms

    def __call__(self):
        return self.ms / 1000


class FakeAster:
    """Enough of Aster for the trader: marks, one-way positions, trigger orders that fire on the mark."""

    def __init__(self, can_trade=True):
        self.can_trade = can_trade
        self.signer = "0xS" if can_trade else None
        self.marks = {}
        self.pos = {}
        self.orders = {}          # oid -> dict
        self.income_rows = []
        self.avail = 1000.0
        self.n = 0
        self.calls = []
        self.fail_next_order = None

    def specs(self):
        return {s: (Decimal("0.0001"), Decimal("0.1"), Decimal("5")) for s in self.marks}

    def premium(self):
        return {s: (px, 0.0001, T0 + 8 * H) for s, px in self.marks.items()}

    def book(self, sym):
        return self.marks[sym] * 0.999, self.marks[sym]

    def positions(self):
        return {s: q for s, q in self.pos.items() if q != 0}

    def available(self):
        return self.avail

    def open_orders(self):
        return [{"orderId": o["orderId"]} for o in self.orders.values() if o["status"] == "NEW"]

    def setup(self, sym, lev):
        self.calls.append(("setup", sym, lev))

    def order(self, **p):
        self.calls.append(("order", p))
        if self.fail_next_order:
            e, self.fail_next_order = self.fail_next_order, None
            raise e
        self.n += 1
        oid = str(self.n)
        sym = p["symbol"]
        if p["type"] == "MARKET":
            q = Decimal(p["quantity"])
            self.pos[sym] = self.pos.get(sym, Decimal(0)) + (q if p["side"] == "BUY" else -q)
            o = {"orderId": oid, "status": "FILLED", "executedQty": p["quantity"], "avgPrice": str(self.marks[sym]),
                 "clientOrderId": p.get("newClientOrderId"), "type": "MARKET", "symbol": sym}
        else:
            o = {"orderId": oid, "status": "NEW", "executedQty": "0", "avgPrice": "0", **p}
        self.orders[oid] = o
        return o

    def get_order(self, sym, oid=None, cid=None):
        if oid:
            return self.orders.get(oid)
        return next((o for o in self.orders.values() if o.get("clientOrderId") == cid), None)

    def cancel_all(self, sym):
        for o in self.orders.values():
            if o.get("symbol") == sym and o["status"] == "NEW":
                o["status"] = "CANCELED"

    def income(self, sym, a, b):
        return self.income_rows

    def tick(self, sym, px):
        """Move the mark and fire any trigger orders it crosses."""
        self.marks[sym] = px
        for o in self.orders.values():
            if o.get("symbol") == sym and o["status"] == "NEW" and px >= float(o["stopPrice"]):
                left = self.pos.get(sym, Decimal(0))
                q = left if o.get("closePosition") else min(left, Decimal(o["quantity"]))
                if q > 0:
                    self.pos[sym] = left - q
                    o.update(status="FILLED", executedQty=str(q), avgPrice=str(px))
                else:
                    o["status"] = "EXPIRED"


@pytest.fixture
def setup(tmp_path):
    wake = tmp_path / "wake"
    wake.mkdir()
    msgs = []
    ax = FakeAster()
    clock = Clock(T0)
    tr = wl.Trader(tmp_path / "live", wake_dir=wake, aster=ax, notify=msgs.append, clock=clock)

    def signal(sym, mark, ts=None, liquid=True):
        ax.marks.setdefault(sym, mark)
        st = json.loads((wake / "wake_watch.json").read_text()) if (wake / "wake_watch.json").exists() else {"active": {}}
        st["active"][sym] = {"ts": ts or clock.ms, "entry": mark, "liquid": liquid}
        (wake / "wake_watch.json").write_text(json.dumps(st))
    return tr, ax, clock, msgs, signal


def test_paper_half_at_30_rest_at_100(setup):
    tr, ax, clock, msgs, signal = setup
    tr.step()                                   # first start
    signal("AAAUSDT", 1.0)
    tr.step()
    st = tr.load()
    assert st["trades"]["AAAUSDT"]["mode"] == "paper"
    assert ax.calls == []                       # paper places nothing
    ax.marks["AAAUSDT"] = 1.35
    clock.ms += H
    tr.step()
    assert tr.load()["trades"]["AAAUSDT"]["left"] == pytest.approx(0.5)
    ax.marks["AAAUSDT"] = 2.05
    clock.ms += H
    tr.step()
    rows = wl.closed_trades(tr.dir)
    assert len(rows) == 1 and rows[0]["reason"] == "take-profits"
    assert float(rows[0]["price_pnl"]) == pytest.approx(100 * (0.5 * 0.3 + 0.5 * 1.0))
    assert "AAAUSDT" not in tr.load()["trades"]


def test_paper_72h_exit_and_ignores_non_liquid(setup):
    tr, ax, clock, msgs, signal = setup
    tr.step()
    signal("AAAUSDT", 1.0)
    signal("BBBUSDT", 1.0, liquid=False)
    tr.step()
    assert set(tr.load()["trades"]) == {"AAAUSDT"}
    ax.marks["AAAUSDT"] = 0.9
    clock.ms += 72 * H
    tr.step()
    r = wl.closed_trades(tr.dir)[0]
    assert r["reason"] == "72h" and float(r["price_pnl"]) == pytest.approx(-10)


def test_skips_stale_old_and_chased(setup):
    tr, ax, clock, msgs, signal = setup
    signal("OLDUSDT", 1.0, ts=T0 - 5 * H)       # before the trader existed
    tr.step()
    assert tr.load()["trades"] == {}
    clock.ms += 2 * H
    signal("STALEUSDT", 1.0, ts=clock.ms - 60 * 60_000)
    ax.marks["RUNUSDT"] = 1.10
    signal("RUNUSDT", 1.0)
    tr.step()
    assert tr.load()["trades"] == {}
    assert any("min old" in m for m in msgs) and any("above the signal" in m for m in msgs)


def test_max_open_cap(setup):
    tr, ax, clock, msgs, signal = setup
    tr.step()
    wl.save_settings(tr.dir, max_open=2)
    for s in ("A1USDT", "A2USDT", "A3USDT"):
        signal(s, 1.0)
    tr.step()
    assert len(tr.load()["trades"]) == 2 and any("the cap is 2" in m for m in msgs)


def test_live_entry_places_two_triggers_and_tp_closes(setup):
    tr, ax, clock, msgs, signal = setup
    tr.step()
    wl.save_settings(tr.dir, mode="live")
    signal("AAAUSDT", 2.0)
    tr.step()
    t = tr.load()["trades"]["AAAUSDT"]
    assert t["status"] == "open" and t["qty"] == "50.0"           # $100 / 2.0 ask, floored to 0.1
    assert ("setup", "AAAUSDT", 2) in ax.calls
    trig = [o for o in ax.orders.values() if o["type"] == "TAKE_PROFIT_MARKET"]
    assert sorted(o["stopPrice"] for o in trig) == ["2.6", "4.0"]
    half = next(o for o in trig if o["stopPrice"] == "2.6")
    assert half["quantity"] == "25.0" and half["reduceOnly"] == "true"
    assert next(o for o in trig if o["stopPrice"] == "4.0")["closePosition"] == "true"
    ax.tick("AAAUSDT", 2.7)
    clock.ms += H
    tr.step()
    assert any("+30% filled" in m for m in msgs)
    ax.tick("AAAUSDT", 4.1)
    ax.income_rows = [{"incomeType": "REALIZED_PNL", "income": "60"}, {"incomeType": "COMMISSION", "income": "-0.2"},
                      {"incomeType": "FUNDING_FEE", "income": "-1.5"}]
    clock.ms += H
    tr.step()
    r = wl.closed_trades(tr.dir)[0]
    assert r["mode"] == "live" and r["reason"] == "take-profits" and float(r["net"]) == pytest.approx(58.3)


def test_live_72h_cancels_triggers_and_sells(setup):
    tr, ax, clock, msgs, signal = setup
    tr.step()
    wl.save_settings(tr.dir, mode="live")
    signal("AAAUSDT", 1.0)
    tr.step()
    clock.ms += 72 * H
    ax.marks["AAAUSDT"] = 0.8
    tr.step()
    assert tr.load()["trades"]["AAAUSDT"]["status"] == "closing"
    assert ax.pos["AAAUSDT"] == 0
    assert all(o["status"] != "NEW" for o in ax.orders.values())
    sell = [c[1] for c in ax.calls if c[0] == "order" and c[1]["side"] == "SELL" and c[1]["type"] == "MARKET"]
    assert sell and sell[0]["reduceOnly"] == "true"
    ax.income_rows = [{"incomeType": "REALIZED_PNL", "income": "-20"}]
    clock.ms += 60_000
    tr.step()
    r = wl.closed_trades(tr.dir)[0]
    assert r["reason"] == "72h" and float(r["net"]) == -20


def test_live_liquidation_and_brake(setup):
    tr, ax, clock, msgs, signal = setup
    tr.step()
    wl.save_settings(tr.dir, mode="live", halt_loss=40)
    signal("AAAUSDT", 1.0)
    tr.step()
    ax.pos["AAAUSDT"] = Decimal(0)                     # liquidated on the venue
    ax.income_rows = [{"incomeType": "REALIZED_PNL", "income": "-48"}, {"incomeType": "INSURANCE_CLEAR", "income": "-2"}]
    clock.ms += H
    tr.step()
    r = wl.closed_trades(tr.dir)[0]
    assert r["reason"].startswith("closed on Aster") and float(r["net"]) == -50
    assert tr.load()["halted"]
    signal("BBBUSDT", 1.0)
    tr.step()
    assert "BBBUSDT" not in tr.load()["trades"] and any("brake on" in m for m in msgs)
    wl.command(tr.dir, ["resume"])
    clock.ms += 60_000
    signal("CCCUSDT", 1.0)
    tr.step()
    assert "CCCUSDT" in tr.load()["trades"]


def test_live_never_trades_a_coin_with_a_foreign_position(setup):
    tr, ax, clock, msgs, signal = setup
    tr.step()
    wl.save_settings(tr.dir, mode="live")
    ax.pos["AAAUSDT"] = Decimal("-50")                 # e.g. the basis bot's short
    signal("AAAUSDT", 1.0)
    tr.step()
    assert tr.load()["trades"] == {} and tr.load()["foreign"] == ["AAAUSDT"]


def test_live_ambiguous_entry_recovered_by_client_id(setup):
    tr, ax, clock, msgs, signal = setup
    tr.step()
    wl.save_settings(tr.dir, mode="live")
    signal("AAAUSDT", 1.0)
    # the order lands but the response is lost
    real = ax.order

    def lost(**p):
        real(**p)
        ax.order = real
        raise wl.AsterError("timeout", ambiguous=True)
    ax.order = lost
    tr.step()
    t = tr.load()["trades"]["AAAUSDT"]
    assert t["status"] == "open" and Decimal(t["qty"]) == ax.pos["AAAUSDT"]


def test_live_without_key_skips(setup):
    tr, ax, clock, msgs, signal = setup
    ax.can_trade = False
    tr.step()
    wl.save_settings(tr.dir, mode="live")
    signal("AAAUSDT", 1.0)
    tr.step()
    assert tr.load()["trades"] == {} and any("no Aster key" in m for m in msgs)


def test_manual_close_and_command_parsing(setup):
    tr, ax, clock, msgs, signal = setup
    tr.step()
    signal("AAAUSDT", 1.0)
    tr.step()
    assert "closing AAAUSDT" in wl.command(tr.dir, ["close", "aaa"])
    clock.ms += 60_000
    tr.step()
    assert wl.closed_trades(tr.dir)[0]["reason"] == "manual"
    assert "between" in wl.command(tr.dir, ["lev", "9"])
    assert "set to 3" in wl.command(tr.dir, ["lev", "3"]) and wl.load_settings(tr.dir)["lev"] == 3
    assert "LIVE" in wl.command(tr.dir, ["live"])
    assert "Wake live" in wl.command(tr.dir, [])
    assert "/wl on|off" in wl.command(tr.dir, ["help"])


def test_sign_shape():
    pytest.importorskip("eth_account")
    ax = wl.Aster("0x" + "11" * 20, "0x" + "22" * 32)
    p = ax._sign({"symbol": "AAAUSDT"})
    assert p["signer"] == "0x" + "11" * 20 and p["signature"].startswith("0x") and len(p["signature"]) == 132
    assert int(ax._sign({})["nonce"]) > int(p["nonce"])


def test_report_shows_unrealised_per_trade_and_total(setup):
    tr, ax, clock, msgs, signal = setup
    tr.step()
    signal("PPPUSDT", 1.0)                          # paper
    tr.step()
    wl.save_settings(tr.dir, mode="live")
    signal("LLLUSDT", 2.0)                          # live, 50 @ 2.0
    tr.step()
    ax.marks["PPPUSDT"] = 1.1                       # paper +10% on $100
    ax.tick("LLLUSDT", 2.7)                         # live: half (25) sold at 2.7, 25 left
    ax.marks["LLLUSDT"] = 2.4
    clock.ms += H
    tr.step()
    tr.step()                                       # the take-profit fill is seen, then marked at 2.4
    rep = wl.report(tr.dir)
    assert "PPPUSDT [paper]" in rep and "uPnL <b>$+10.00</b>" in rep
    assert "LLLUSDT [live]" in rep and "uPnL <b>$+10.00</b>" in rep and "$+17.50 banked" in rep   # 25 x 0.4, 25 x 0.7
    assert "live unrealised: $+10.00</b> on 1 open" in rep and "paper unrealised: $+10.00</b> on 1 open" in rep
