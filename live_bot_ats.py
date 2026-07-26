#!/usr/bin/env python3
"""
Hyperliquid volume-breakout FADE — REAL-MONEY live bot, `15m-ats` arm.

This is the live version of the `paper_15m_ats` arm (15m bars, HIGH+MID tiers,
range-breakout trigger, notional scaled by the spike's avg-trade-size ratio).

WHY THIS IS NOT JUST paper_bot.py WITH ORDERS
---------------------------------------------
The paper bot books a fill at the best bid/ask instantly, every time, on both
sides. The shadow-fill audit against 5 days of real tape says that is false:
with an order resting for 300s, only ~76% of entries and ~75% of exits ever had
an opposite aggressor print through them. Applying that to the paper arms cut the
book from +$42 booked to roughly flat. So this bot's whole job is to be honest
about execution:

  ENTRY  post-only (Alo) at best on our side, ONE placement, no re-peg. If it has
         not filled when ENTRY_WINDOW_S expires, cancel and ABANDON the signal.
         No chasing. This mirrors the audited rule exactly, so live fill rates are
         directly comparable to the 76% the tape predicted.
  EXIT   exits are mandatory, so they escalate: post-only at best, re-peg on each
         poll while the book moves away, then after EXIT_GRACE_S cross the spread
         with an IOC taker order. Guarantees we exit near the tested 8h horizon at
         the cost of ~4.5bps + spread on the ones that do not fill passively.
         The paper arm never paid this. Expect live < paper because of it.

Everything about *what* to trade is imported from paper_bot.Bot (entry_ok,
exit_reason, size_mult, features, calibrate, load_universe, funding_signs) so the
live arm provably cannot drift from the arm that was measured.

SAFETY
------
Orders are NOT sent unless you pass --live. Default is a dry run that logs every
order it would have placed. Also enforced: per-trade and gross notional caps, a
position cap, a daily realized-loss limit, a $10 minimum order, and a kill switch
(`touch <datadir>/KILL` -> flatten everything at market and exit).

SETUP
-----
  pip install hyperliquid-python-sdk
  export HL_ACCOUNT_ADDRESS=0x...      # the account that holds the funds
  export HL_SECRET_KEY=0x...           # an API WALLET key, not your main key
Generate an API wallet at https://app.hyperliquid.xyz/API — it can trade but
cannot withdraw, which is what you want on a server.

  python3 live_bot_ats.py --datadir ./live_15m_ats               # dry run
  python3 live_bot_ats.py --datadir ./live_15m_ats --live        # arm it
"""
import argparse, csv, json, os, time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime, timezone

import paper_bot
from paper_bot import Bot, now_ms, iso

try:
    import telegram_notify as tg
except Exception:
    tg = None

# ---- execution constants (the part the paper bot got wrong) ----
ENTRY_WINDOW_S = 300      # how long an entry order rests before we abandon the signal
EXIT_GRACE_S   = 600      # passive exit attempts for this long, then cross the spread
POLL_S         = 20       # order-management poll interval
TAKER_SLIP     = 0.004    # IOC limit offset when crossing (0.4%) — a price cap, not a target
MIN_NOTIONAL   = 10.0     # Hyperliquid perp minimum order value (USD)
PERP_MAX_DEC   = 6        # MAX_DECIMALS for perps; px decimals <= PERP_MAX_DEC - szDecimals
MAINNET        = "https://api.hyperliquid.xyz"


# ---------- tick / lot rounding ----------
def _grid_decimals(px, sz_dec):
    """Decimals allowed for a perp price: 5 significant figures AND <= 6 - szDecimals."""
    if px <= 0:
        return 0
    exp = Decimal(str(px)).adjusted()          # floor(log10(px))
    sig_dec = 4 - exp                          # 5 sig figs
    return max(0, min(PERP_MAX_DEC - sz_dec, sig_dec))


def round_px(px, sz_dec, is_buy):
    """Round a maker price onto the legal grid, AWAY from the spread.

    Direction matters: rounding a resting buy *up* can cross the ask (Alo would
    then be rejected outright), and a resting sell *down* can cross the bid. So
    buys floor and sells ceil.
    """
    dec = _grid_decimals(px, sz_dec)
    q = Decimal(1).scaleb(-dec)
    d = Decimal(str(px)).quantize(q, rounding=ROUND_DOWN if is_buy else ROUND_UP)
    return float(d)


def round_sz(sz, sz_dec):
    q = Decimal(1).scaleb(-sz_dec)
    return float(Decimal(str(sz)).quantize(q, rounding=ROUND_DOWN))


class LiveBot(Bot):
    def __init__(self, datadir, live=False, notional=None, max_gross=1000.0,
                 max_positions=10, daily_loss_limit=50.0, leverage=3, size_by_ats=True):
        # 15m, HIGH+MID, breakout trigger. size_by_ats is the A/B knob: the tape says
        # whale-sizing is the worst of {inverse, flat, ats}, but on only 5 days and with
        # nothing significant, so run flat as a second live arm and let real fills decide.
        super().__init__("15m", datadir, tiers=("HIGH", "MID"),
                         trigger="breakout", size_by_ats=size_by_ats)
        self.live = live
        self.max_gross = max_gross
        self.max_positions = max_positions
        self.daily_loss_limit = daily_loss_limit
        self.leverage = int(leverage)
        if notional:
            paper_bot.NOTIONAL = notional
        self.notional = paper_bot.NOTIONAL
        # derive from the datadir so parallel arms are distinguishable in Telegram
        base = os.path.basename(os.path.normpath(datadir))
        self.label = ("LIVE-" + base.replace("live_", "", 1).replace("_", "-")
                      + ("" if live else "-dry"))

        self.pending = {}        # sym -> pending ENTRY order
        self.exiting = {}        # sym -> pending EXIT order state
        self.sz_dec = {}         # sym -> szDecimals
        self.lev_set = set()
        self.day_pnl = 0.0
        self.day = datetime.now(timezone.utc).date()
        self.kill_file = os.path.join(datadir, "KILL")

        # signals we abandoned because the entry never filled — the live fill-rate log
        self.miss_csv = os.path.join(datadir, "missed_15m_ats.csv")
        if not os.path.exists(self.miss_csv):
            with open(self.miss_csv, "w", newline="") as f:
                csv.writer(f).writerow(["time", "symbol", "side", "px", "sz",
                                        "rested_s", "vratio", "rv", "ats_ratio"])
        # live-only columns appended after the paper schema so existing analysis
        # scripts (shadow_fill2.py, analysis/*) still parse this file unchanged
        self._ensure_live_cols()
        self._connect()

    def _ensure_live_cols(self):
        with open(self.trade_csv) as f:
            hdr = f.readline().strip().split(",")
        if "fee_usd" in hdr:
            return
        rows = list(csv.reader(open(self.trade_csv)))
        rows[0] += ["fee_usd", "entry_wait_s", "exit_wait_s", "exit_taker", "repegs", "sz"]
        with open(self.trade_csv, "w", newline="") as f:
            csv.writer(f).writerows(rows)

    # ---------- exchange plumbing ----------
    def _connect(self):
        # market data comes via paper_bot.hl_post, which retries 429s with exponential
        # backoff — important here, since aborting a cycle means an open position goes
        # an extra 15 minutes without an exit check.
        addr = os.environ.get("HL_ACCOUNT_ADDRESS")
        secret = os.environ.get("HL_SECRET_KEY")
        if not addr:
            raise SystemExit("HL_ACCOUNT_ADDRESS not set")
        self.address = addr
        from hyperliquid.info import Info
        self.info = Info(MAINNET, skip_ws=True)
        for a in self.info.meta()["universe"]:
            self.sz_dec[a["name"]] = int(a["szDecimals"])
        self.ex = None
        if self.live:
            if not secret:
                raise SystemExit("HL_SECRET_KEY not set (needed with --live)")
            import eth_account
            from hyperliquid.exchange import Exchange
            wallet = eth_account.Account.from_key(secret)
            self.ex = Exchange(wallet, MAINNET, account_address=addr)
            self.log(f"exchange armed for {addr} via API wallet {wallet.address}")
        else:
            self.log("DRY RUN — no orders will be sent (pass --live to arm)")

    def equity(self):
        try:
            return float(self.info.user_state(self.address)["marginSummary"]["accountValue"])
        except Exception as e:
            self.log(f"WARN equity: {e}")
            return None

    def exchange_positions(self):
        """sym -> signed size, straight from the exchange. The only source of truth.

        Returns None if the QUERY FAILED. An empty dict means "genuinely flat", and
        reconcile() must never confuse the two: on a transient 429 an empty dict would
        look like every position had vanished, and we would drop real positions and
        stop managing them. Fail closed instead.
        """
        try:
            out = {}
            for ap in self.info.user_state(self.address).get("assetPositions", []):
                p = ap["position"]
                szi = float(p["szi"])
                if szi != 0:
                    out[p["coin"]] = szi
            return out
        except Exception as e:
            self.log(f"WARN positions: {e}")
            return None

    def _set_leverage(self, sym):
        if sym in self.lev_set or not self.live:
            return
        try:
            self.ex.update_leverage(self.leverage, sym, is_cross=False)
            self.lev_set.add(sym)
        except Exception as e:
            self.log(f"WARN leverage {sym}: {e}")

    def _place(self, sym, is_buy, sz, px, tif, reduce_only=False):
        """Return (oid, filled_sz, avg_px). oid None if nothing rests."""
        if not self.live:
            self.log(f"  DRY {'BUY ' if is_buy else 'SELL'} {sym} sz={sz} @ {px} {tif}"
                     f"{' RO' if reduce_only else ''}")
            return (-1, 0.0, None)          # -1 = phantom resting order
        try:
            r = self.ex.order(sym, is_buy, sz, px, {"limit": {"tif": tif}},
                              reduce_only=reduce_only)
        except Exception as e:
            self.log(f"WARN order {sym}: {e}")
            return (None, 0.0, None)
        if r.get("status") != "ok":
            self.log(f"WARN order {sym} rejected: {r}")
            return (None, 0.0, None)
        st = r["response"]["data"]["statuses"][0]
        if "resting" in st:
            return (st["resting"]["oid"], 0.0, None)
        if "filled" in st:
            f = st["filled"]
            return (f.get("oid"), float(f["totalSz"]), float(f["avgPx"]))
        self.log(f"WARN order {sym} status: {st}")      # e.g. Alo would have crossed
        return (None, 0.0, None)

    def _cancel(self, sym, oid):
        if not self.live or oid in (None, -1):
            return
        try:
            self.ex.cancel(sym, oid)
        except Exception as e:
            self.log(f"WARN cancel {sym} {oid}: {e}")

    def _filled_sz(self, sym, oid, want_sz):
        """How much of our resting order has filled, and at what average price."""
        if not self.live:
            # Dry run: assume the paper bot's optimistic instant fill, purely so the
            # rest of the path (promote -> hold -> exit -> book) is exercisable offline.
            # This is the assumption the tape audit disproved — never read dry-run P&L
            # as an estimate of live P&L.
            return (want_sz, None)
        if oid in (None, -1):
            return (0.0, None)
        try:
            st = self.info.query_order_by_oid(self.address, oid)
            o = st.get("order", {}).get("order", {})
            rem = float(o.get("sz", 0))                 # sz is the REMAINING size
            filled = max(0.0, want_sz - rem)
            status = st.get("order", {}).get("status")
            if status == "filled":
                filled = want_sz
            return (filled, None)
        except Exception as e:
            self.log(f"WARN query oid {sym}/{oid}: {e}")
            return (0.0, None)

    def _fees_since(self, sym, since_ms):
        """Actual USD fees paid on this coin since a timestamp (live fees, not assumed)."""
        if not self.live:
            return None
        try:
            fills = self.info.user_fills_by_time(self.address, since_ms - 1000)
            return sum(float(f.get("fee", 0)) for f in fills if f.get("coin") == sym)
        except Exception as e:
            self.log(f"WARN fills {sym}: {e}")
            return None

    # ---------- entry ----------
    def open_pos(self, sym, brk, feat):
        """Place a post-only entry. Does NOT create a position — manage_pending() does,
        and only if the order actually fills inside ENTRY_WINDOW_S."""
        if sym in self.pending or sym in self.positions or sym in self.exiting:
            return
        if len(self.positions) + len(self.pending) >= self.max_positions:
            return
        gross = sum(p["notional"] for p in self.positions.values())
        mult = self.size_mult(feat)
        notional = self.notional * mult
        if gross + notional > self.max_gross:
            self.log(f"SKIP {sym}: gross cap (${gross:.0f} + ${notional:.0f} > ${self.max_gross:.0f})")
            return
        if self.day_pnl <= -abs(self.daily_loss_limit):
            self.log(f"SKIP {sym}: daily loss limit hit (${self.day_pnl:+.2f})")
            return
        ba = self.best_bid_ask(sym)
        if ba is None:
            return
        bid, ask = ba
        is_buy = brk < 0                        # fade: down-breakout -> LONG
        raw_px = bid if is_buy else ask
        sd = self.sz_dec.get(sym, 2)
        px = round_px(raw_px, sd, is_buy)
        sz = round_sz(notional / px, sd)
        if px <= 0 or sz <= 0 or sz * px < MIN_NOTIONAL:
            self.log(f"SKIP {sym}: unrepresentable or sub-minimum "
                     f"(px={px} sz={sz} ntl=${sz*px:.2f} < ${MIN_NOTIONAL})")
            return
        self._set_leverage(sym)
        oid, fsz, fpx = self._place(sym, is_buy, sz, px, "Alo")
        if oid is None and fsz == 0:
            return
        self.pending[sym] = {
            "oid": oid, "is_buy": is_buy, "px": px, "sz": sz, "filled": fsz,
            "placed_ms": now_ms(), "dir": 1 if is_buy else -1, "notional": notional,
            "mult": mult, "prior_h": feat["prior_h"], "prior_l": feat["prior_l"],
            "entry_bid": bid, "entry_ask": ask, "vratio": feat["vratio"],
            "rv": feat["rv"], "ats_ratio": feat.get("ats_ratio"),
        }
        self.log(f"PLACE {sym:12s} {'BUY ' if is_buy else 'SELL':4s} sz={sz} @ {px:.6g}  "
                 f"(vr={feat['vratio']:.1f}x rv={feat['rv']:.4f} size={mult:.1f}x "
                 f"${notional:.0f})  resting up to {ENTRY_WINDOW_S}s")
        self.notify(f"⏳ PLACE {'LONG' if is_buy else 'SHORT'} <b>{sym}</b> @ {px:.6g}\n"
                    f"size={mult:.1f}x (${notional:.0f})  vr={feat['vratio']:.1f}x")

    def _promote(self, sym, pend, filled):
        """An entry order filled (fully or partially) -> we now hold a real position."""
        notional = filled * pend["px"]
        self.positions[sym] = {
            "dir": pend["dir"], "entry_px": pend["px"], "entry_ms": now_ms(),
            "prior_h": pend["prior_h"], "prior_l": pend["prior_l"],
            "entry_bid": pend["entry_bid"], "entry_ask": pend["entry_ask"],
            "notional": notional, "sz": filled,
            "entry_wait_s": round((now_ms() - pend["placed_ms"]) / 1000, 1),
            "fee_usd": self._fees_since(sym, pend["placed_ms"]) or 0.0,
        }
        part = "" if abs(filled - pend["sz"]) < 1e-12 else f" PARTIAL {filled}/{pend['sz']}"
        self.log(f"OPEN  {sym:12s} {'LONG ' if pend['dir'] > 0 else 'SHORT':5s} @ {pend['px']:.6g}  "
                 f"sz={filled} (${notional:.0f}){part}  "
                 f"waited={self.positions[sym]['entry_wait_s']}s  open={len(self.positions)}")
        self.notify(f"\U0001F7E2 FILLED {'LONG' if pend['dir'] > 0 else 'SHORT'} <b>{sym}</b> "
                    f"@ {pend['px']:.6g}\nsz={filled} (${notional:.0f}){part}  open={len(self.positions)}")
        self._save_state()

    def _abandon(self, sym, pend):
        """Entry window expired unfilled. Cancel, log the miss, do NOT chase."""
        self._cancel(sym, pend["oid"])
        rested = round((now_ms() - pend["placed_ms"]) / 1000, 1)
        with open(self.miss_csv, "a", newline="") as f:
            csv.writer(f).writerow([iso(now_ms()), sym,
                                    "LONG" if pend["dir"] > 0 else "SHORT",
                                    f"{pend['px']:.8g}", pend["sz"], rested,
                                    f"{pend['vratio']:.2f}", f"{pend['rv']:.6f}",
                                    "" if pend["ats_ratio"] is None else f"{pend['ats_ratio']:.2f}"])
        self.log(f"MISS  {sym:12s} unfilled after {rested}s @ {pend['px']:.6g} — signal abandoned")
        self.notify(f"⚪ MISS <b>{sym}</b> — no fill in {rested}s, skipped")

    # ---------- exit ----------
    def close_pos(self, sym, reason, forced_px=None):
        """Begin a mandatory exit: passive first, then cross. Books P&L on fill."""
        if sym in self.exiting:
            return
        p = self.positions[sym]
        # done   = size filled by earlier (already cancelled) exit orders
        # order_sz = size of the order currently resting, so a partial fill on a
        #            re-pegged order is never counted twice
        self.exiting[sym] = {"reason": reason, "started_ms": now_ms(), "oid": None,
                             "px": None, "repegs": 0, "done": 0.0, "order_sz": 0.0}
        self.log(f"EXIT  {sym:12s} {reason} — working passively (grace {EXIT_GRACE_S}s)")
        self._work_exit(sym)

    def _work_exit(self, sym):
        """One pass of the exit escalation ladder."""
        p, ex = self.positions[sym], self.exiting[sym]
        sd = self.sz_dec.get(sym, 2)
        remaining = round_sz(p["sz"] - ex["done"], sd)
        if remaining <= 0:
            self._book_exit(sym, ex["px"] or p["entry_px"], taker=False)
            return
        is_buy = p["dir"] < 0                   # short closes by buying
        ba = self.best_bid_ask(sym)
        if ba is None:
            return
        bid, ask = ba
        elapsed = (now_ms() - ex["started_ms"]) / 1000

        if elapsed >= EXIT_GRACE_S:
            # escalate: cancel the resting order and cross the spread
            self._cancel(sym, ex["oid"])
            cap = ask * (1 + TAKER_SLIP) if is_buy else bid * (1 - TAKER_SLIP)
            px = round_px(cap, sd, not is_buy)   # cap must be permissive -> round outward
            oid, fsz, fpx = self._place(sym, is_buy, remaining, px, "Ioc", reduce_only=True)
            if fsz > 0:
                self.log(f"  crossed {sym} sz={fsz} @ {fpx} after {elapsed:.0f}s passive")
                self._book_exit(sym, fpx, taker=True)
            else:
                self.log(f"WARN {sym}: IOC exit got no fill, retrying next poll")
            return

        want = bid if is_buy else ask
        px = round_px(want, sd, is_buy)
        if ex["oid"] is not None and abs(px - (ex["px"] or 0)) < 1e-12:
            return                               # already resting at the right price
        if ex["oid"] is not None:                # book moved -> re-peg
            # bank whatever the outgoing order filled before replacing it
            filled_on_old, _ = self._filled_sz(sym, ex["oid"], ex["order_sz"])
            ex["done"] = min(p["sz"], ex["done"] + filled_on_old)
            self._cancel(sym, ex["oid"])
            ex["repegs"] += 1
            remaining = round_sz(p["sz"] - ex["done"], sd)
            if remaining <= 0:
                self._book_exit(sym, px, taker=False)
                return
        oid, fsz, fpx = self._place(sym, is_buy, remaining, px, "Alo", reduce_only=True)
        ex["oid"], ex["px"], ex["order_sz"] = oid, px, remaining
        if fsz > 0:
            ex["done"] = min(p["sz"], ex["done"] + fsz)
            self._book_exit(sym, fpx or px, taker=False)

    def _book_exit(self, sym, exit_px, taker):
        """Write the closed trade. Mirrors paper_bot's schema, with real fees appended."""
        p = self.positions.pop(sym)
        ex = self.exiting.pop(sym, {})
        d, reason = p["dir"], ex.get("reason", "exit")
        sz = p["sz"]
        gross = d * (exit_px - p["entry_px"]) / p["entry_px"]
        fee_usd = self._fees_since(sym, p["entry_ms"] - 60000)
        if fee_usd is None:                      # dry run: fall back to the modelled fee
            fee_usd = p["notional"] * paper_bot.MAKER_FEE * 2
        fee = fee_usd / p["notional"] if p["notional"] else 0.0
        net = gross - fee
        pnl = p["notional"] * gross - fee_usd
        self.cum_pnl += pnl
        self.day_pnl += pnl
        self.n_closed += 1
        self.n_win += 1 if pnl > 0 else 0
        hold_h = (now_ms() - p["entry_ms"]) / 3600000
        ba = self.best_bid_ask(sym) or (exit_px, exit_px)
        if taker:
            reason += "+taker"
        with open(self.trade_csv, "a", newline="") as f:
            csv.writer(f).writerow([
                iso(now_ms()), sym, "SHORT" if d < 0 else "LONG", iso(p["entry_ms"]),
                f"{p['entry_px']:.8g}", f"{exit_px:.8g}", f"{hold_h:.2f}",
                f"{gross*1e4:.1f}", f"{fee*1e4:.1f}", f"{net*1e4:.1f}", f"{pnl:.4f}",
                reason, p["entry_bid"], p["entry_ask"], ba[0], ba[1], f"{self.cum_pnl:.4f}",
                f"{fee_usd:.4f}", p.get("entry_wait_s", ""),
                round((now_ms() - ex.get("started_ms", now_ms())) / 1000, 1),
                int(taker), ex.get("repegs", 0), sz])
        self.log(f"CLOSE {sym:12s} {reason:14s} net={net*1e4:+6.1f}bps pnl=${pnl:+.3f} "
                 f"fee=${fee_usd:.3f} hold={hold_h:.1f}h  cum=${self.cum_pnl:+.2f} "
                 f"day=${self.day_pnl:+.2f} trades={self.n_closed}")
        emoji = "\U0001F534" if pnl <= 0 else "\U0001F535"      # red loss / blue win
        self.notify(f"{emoji} CLOSE <b>{sym}</b> ({reason})\n"
                    f"net={net*1e4:+.1f}bps  pnl=${pnl:+.3f}  fee=${fee_usd:.3f}  hold={hold_h:.1f}h\n"
                    f"cum=${self.cum_pnl:+.2f}  day=${self.day_pnl:+.2f}  win={self._winrate():.0f}%")
        self._save_state()

    # ---------- order management (runs between bars) ----------
    def manage_pending(self):
        for sym in list(self.pending.keys()):
            pend = self.pending[sym]
            filled, _ = self._filled_sz(sym, pend["oid"], pend["sz"])
            filled = max(filled, pend["filled"])
            age = (now_ms() - pend["placed_ms"]) / 1000
            if filled >= pend["sz"] - 1e-12:
                self.pending.pop(sym)
                self._promote(sym, pend, pend["sz"])
            elif age >= ENTRY_WINDOW_S:
                self.pending.pop(sym)
                if filled > 0:                   # keep the partial, cancel the rest
                    self._cancel(sym, pend["oid"])
                    self._promote(sym, pend, round_sz(filled, self.sz_dec.get(sym, 2)))
                else:
                    self._abandon(sym, pend)
            else:
                pend["filled"] = filled
        for sym in list(self.exiting.keys()):
            if sym not in self.positions:
                self.exiting.pop(sym, None)
                continue
            try:
                ex, p = self.exiting[sym], self.positions[sym]
                # fills on the CURRENTLY resting order only; ex["done"] holds the rest
                cur, _ = self._filled_sz(sym, ex["oid"], ex["order_sz"])
                if ex["done"] + cur >= p["sz"] - 1e-12:
                    ex["done"] = p["sz"]
                    # ex["px"] is the price we were resting at, i.e. the fill price
                    self._book_exit(sym, ex["px"] or p["entry_px"], taker=False)
                else:
                    self._work_exit(sym)
            except Exception as e:
                self.log(f"WARN exit {sym}: {e}")

    def check_kill(self):
        if not os.path.exists(self.kill_file):
            return False
        self.log("KILL file present — cancelling orders and flattening at market")
        self.notify("\U0001F6D1 KILL — flattening all positions")
        for sym, pend in list(self.pending.items()):
            self._cancel(sym, pend["oid"])
            self.pending.pop(sym)
        for sym in list(self.positions.keys()):
            ex = self.exiting.get(sym)
            if ex:
                self._cancel(sym, ex["oid"])
            self.exiting[sym] = {"reason": "kill", "started_ms": 0, "oid": None,
                                 "px": None, "repegs": 0, "done": 0.0, "order_sz": 0.0}
            try:
                self._work_exit(sym)             # started_ms=0 -> immediate cross
            except Exception as e:
                self.log(f"WARN kill exit {sym}: {e}")
        self._save_state()
        return True

    def reconcile(self):
        """Trust the exchange over local state. Catches restarts, manual trades,
        and exchange-side liquidations (which the paper bot could only model)."""
        if not self.live:
            return
        live_pos = self.exchange_positions()
        if live_pos is None:
            # could not read the exchange; keep managing what we think we hold
            self.log("RECONCILE skipped: exchange state unreadable (keeping local positions)")
            return
        for sym in list(self.positions.keys()):
            # a just-filled position can lag in user_state; don't drop it on a race
            if (now_ms() - self.positions[sym]["entry_ms"]) < 120_000:
                continue
            if sym not in live_pos:
                p = self.positions.pop(sym)
                self.exiting.pop(sym, None)
                self.log(f"RECONCILE {sym}: gone on exchange (liquidated or closed "
                         f"externally) — dropping local position, P&L NOT booked")
                self.notify(f"⚠️ RECONCILE <b>{sym}</b> vanished on exchange "
                            f"(liquidation?) — check your fills")
        for sym, szi in live_pos.items():
            if sym not in self.positions:
                self.log(f"RECONCILE {sym}: exchange holds {szi} but bot has no record — "
                         f"NOT adopting. Close it manually or it will sit unmanaged.")
                self.notify(f"⚠️ RECONCILE unmanaged <b>{sym}</b> szi={szi}")
        self._save_state()

    # ---------- main loop ----------
    def cycle(self):
        """Bar-close pass: exits, then entries. Mirrors paper_bot.Bot.cycle()."""
        self.reconcile()
        fsign = self.funding_signs()
        for sym in list(self.positions.keys()):
            if sym in self.exiting:
                continue
            try:
                feat = self.features(self.candles(sym, self.win + 5))
            except Exception as e:
                self.log(f"WARN candles {sym}: {e}")
                continue
            reason = self.exit_reason(self.positions[sym], feat)
            if reason:
                try:
                    self.close_pos(sym, reason)
                except Exception as e:
                    self.log(f"WARN close {sym}: {e}")
        n_sig, last = 0, time.time()
        for sym, tier in self.universe.items():
            if tier not in self.tiers: continue
            if sym in self.positions or sym in self.pending or sym in self.exiting: continue
            if len(self.positions) + len(self.pending) >= self.max_positions: break
            try:
                feat = self.features(self.candles(sym, self.win + 5))
            except Exception:
                continue
            if not self.entry_ok(feat, fsign, sym):
                continue
            n_sig += 1
            try:
                self.open_pos(sym, feat["brk"], feat)
            except Exception as e:
                self.log(f"WARN open {sym}: {e}")
            # the universe scan is slow (~180 serial REST calls); keep resting
            # orders managed while it runs so entry windows are not overrun
            if time.time() - last > POLL_S:
                try: self.manage_pending()
                except Exception as e: self.log(f"WARN manage: {e}")
                last = time.time()
        eq = self.equity()
        self.log(f"cycle done: {n_sig} signals, {len(self.positions)} open, "
                 f"{len(self.pending)} resting, cum=${self.cum_pnl:+.2f}, "
                 f"day=${self.day_pnl:+.2f}" + (f", equity=${eq:,.2f}" if eq else ""))

    def run(self):
        mode = "LIVE — REAL MONEY" if self.live else "DRY RUN"
        self.log(f"=== live bot [15m-ats] {mode} | notional=${self.notional} x(0.5-3.0) "
                 f"lev={self.leverage}x isolated | entry_window={ENTRY_WINDOW_S}s "
                 f"exit_grace={EXIT_GRACE_S}s | caps: {self.max_positions}pos "
                 f"${self.max_gross:.0f}gross ${self.daily_loss_limit:.0f}daily-loss ===")
        self.notify(f"\U0001F916 <b>{mode}</b> 15m-ats starting\n"
                    f"resuming: cum=${self.cum_pnl:+.2f} open={len(self.positions)} "
                    f"closed={self.n_closed}")
        self.load_universe()
        self.reconcile()
        self.calibrate()
        next_eval = 0
        while True:
            if self.check_kill():
                self.log("killed — exiting"); return
            today = datetime.now(timezone.utc).date()
            if today != self.day:
                self.day, self.day_pnl = today, 0.0
            try:
                self.manage_pending()
            except Exception as e:
                self.log(f"ERROR manage: {e}")
            if now_ms() >= next_eval:
                t0 = time.time()
                try:
                    self.cycle()
                except Exception as e:
                    self.log(f"ERROR cycle: {e}")
                    self.notify(f"⚠️ cycle error: {e}")
                self.log(f"cycle took {time.time()-t0:.1f}s")
                nb = (now_ms() // self.bar_ms + 1) * self.bar_ms
                next_eval = nb + paper_bot.POLL_OFFSET_S * 1000
                self._maybe_daily_summary()
                if int(time.time()) % 86400 < self.bar_min * 60:    # re-tier daily, as paper does
                    try: self.load_universe()
                    except Exception: pass
            time.sleep(POLL_S)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Real-money live 15m-ats arm")
    ap.add_argument("--datadir", default="./live_15m_ats")
    ap.add_argument("--live", action="store_true",
                    help="ARM IT — actually send orders. Without this it is a dry run.")
    ap.add_argument("--notional", type=float, default=None,
                    help=f"base USD notional per trade, scaled 0.5-3.0x by ats "
                         f"(default {paper_bot.NOTIONAL})")
    ap.add_argument("--max-gross", type=float, default=1000.0,
                    help="cap on total open notional (USD)")
    ap.add_argument("--max-positions", type=int, default=10)
    ap.add_argument("--daily-loss-limit", type=float, default=50.0,
                    help="stop opening new positions once realized P&L today is below -X")
    ap.add_argument("--leverage", type=int, default=3, help="isolated leverage")
    a = ap.parse_args()
    LiveBot(a.datadir, live=a.live, notional=a.notional, max_gross=a.max_gross,
            max_positions=a.max_positions, daily_loss_limit=a.daily_loss_limit,
            leverage=a.leverage).run()
