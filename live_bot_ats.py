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
from paper_bot import Bot, now_ms, iso, SIZE_MIN, SIZE_MAX

try:
    import telegram_notify as tg
except Exception:
    tg = None

# ---- execution constants (the part the paper bot got wrong) ----
ENTRY_WINDOW_S = 300      # how long an entry order rests before we abandon the signal
EXIT_GRACE_S   = 600      # passive exit attempts for this long, then cross the spread
POLL_S         = 20       # order-management poll interval
# Do not ask the exchange about an order younger than this. orderStatus answers
# {"status":"unknownOid"} until the order is indexed, and a reply with no order in it
# cannot be distinguished from "nothing left to fill" without care. Waiting one poll
# costs nothing; not waiting invented a MET position on 2026-07-30 that never existed.
FILL_QUERY_MIN_S = 5
TAKER_SLIP     = 0.004    # IOC limit offset when crossing (0.4%) — a price cap, not a target
MIN_NOTIONAL   = 10.0     # Hyperliquid perp minimum order value (USD)
# Cross the spread immediately instead of resting when the spread is at or below this,
# in bps of mid. 0 disables (always rest). See analysis/spread_gate.py: on 400 audited
# trades this went +$59.39 -> +$73.63 (+24%) and lifted signals traded from 294 to 343.
# 5 rather than the measured-best 8 because the gain is a broad plateau (+14.23 at 5,
# +15.40 at 8) and it inverts above 8, so 5 sits further from the cliff.
CROSS_SPREAD_BPS = 5.0
# Slippage cap for a crossing ENTRY, bps beyond the far touch. Deliberately far tighter
# than TAKER_SLIP (which is 40bps and exists for mandatory exits): the whole edge is ~45bps,
# so a fill 40bps worse than intended would defeat the point. An entry is optional -- if the
# book moved, taking no fill and losing the signal is strictly better than a bad fill.
CROSS_CAP_BPS  = 10.0
# --- flow-toxicity shadow logging (analysis/toxicity.py) ---
# Recorded at signal time, NEVER acted on. The tape study found that dropping the most
# toxic 20% of fills moved a broad spike population from -259 to +7518 total net bps,
# but with t=0.6-1.1 and on a looser event set than this bot trades. So log it on real
# fills until it is proven, then decide. Requires the tape logger writing to TAPE_DIR.
TAPE_DIR       = "/opt/hyperdata/tape"
TAPE_TAIL_MB   = 12       # tail of today's tape to read per cycle (~3h of prints)
FLOW_MINS      = 60       # trailing window for vpin60 / adverse_ofi
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
                 max_positions=10, daily_loss_limit=50.0, leverage=3, size_by_ats=True,
                 tape_dir=None, max_per_side=20):
        # 15m, HIGH+MID, breakout trigger. size_by_ats is the A/B knob: the tape says
        # whale-sizing is the worst of {inverse, flat, ats}, but on only 5 days and with
        # nothing significant, so run flat as a second live arm and let real fills decide.
        super().__init__("15m", datadir, tiers=("HIGH", "MID"),
                         trigger="breakout", size_by_ats=size_by_ats)
        self.live = live
        self.max_gross = max_gross
        self.max_positions = max_positions
        self.max_per_side = max_per_side
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
        self.max_lev = {}        # sym -> exchange max leverage (128 perps cap at 3x)
        self.lev_set = set()
        self.day_pnl = 0.0
        self.n_capped = 0        # signals refused by the position/gross caps, per cycle
        self.day = datetime.now(timezone.utc).date()
        self.kill_file = os.path.join(datadir, "KILL")
        self.tape_dir = tape_dir or TAPE_DIR
        self.flow = {}           # coin -> {minute: [buy_ntl, sell_ntl]}, shadow only

        # signals we abandoned because the entry never filled — the live fill-rate log
        self.miss_csv = os.path.join(datadir, "missed_15m_ats.csv")
        if not os.path.exists(self.miss_csv):
            with open(self.miss_csv, "w", newline="") as f:
                csv.writer(f).writerow(self.MISS_COLS)
        # live-only columns appended after the paper schema so existing analysis
        # scripts (shadow_fill2.py, analysis/*) still parse this file unchanged
        self._ensure_live_cols()
        self._connect()

    LIVE_COLS = ["fee_usd", "entry_wait_s", "exit_wait_s", "exit_taker", "repegs", "sz",
                 "vpin30", "vpin60", "adverse_ofi",
                 "queue_usd", "queue_ratio", "spread_bps", "crossed", "tier",
                 "ats_ratio"]
    MISS_COLS = ["time", "symbol", "side", "px", "sz", "rested_s", "vratio", "rv",
                 "ats_ratio", "vpin30", "vpin60", "adverse_ofi",
                 "queue_usd", "queue_ratio", "spread_bps", "crossed", "tier"]

    @staticmethod
    def _q3(d):
        """queue/spread/crossed/tier fields as CSV-safe strings, in LIVE_COLS order.

        `crossed` lets live data confirm or refute the 5bps threshold. `tier` is the
        bot's OWN tier at signal time -- reconstructing it afterwards from current
        volumes is unreliable, since ~10% of names cross a tertile boundary within days
        (19 of paper_15m's 180 trades classify as LOW today, in a HIGH+MID-only arm).
        """
        return [("" if d.get(k) is None else f"{d[k]:.4f}")
                for k in ("queue_usd", "queue_ratio", "spread_bps")] + \
               [str(int(d.get("crossed", 0))), str(d.get("tier") or "")]

    @staticmethod
    def _extend_header(path, want):
        """Append any missing columns to an existing CSV header, idempotently.
        Old rows keep fewer fields; DictReader fills them as None, which is fine."""
        if not os.path.exists(path):
            return
        rows = list(csv.reader(open(path)))
        if not rows:
            return
        missing = [c for c in want if c not in rows[0]]
        if not missing:
            return
        rows[0] += missing
        with open(path, "w", newline="") as f:
            csv.writer(f).writerows(rows)

    def _ensure_live_cols(self):
        self._extend_header(self.trade_csv, self.LIVE_COLS)
        self._extend_header(self.miss_csv, self.MISS_COLS)

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
            if a.get("maxLeverage"):
                self.max_lev[a["name"]] = int(a["maxLeverage"])
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
        """Set isolated leverage. Returns True ONLY if the exchange confirmed it.

        This is not best-effort, and the caller must not trade without it. If the call
        fails the coin silently keeps whatever leverage the account already had, which
        can be much HIGHER than intended -- 35 perps allow 10x, one allows 40x. On a
        strategy with no stop-loss and an 8h hold, that is the difference between
        liquidating on a ~28% adverse move and a ~5% one.

        Observed in production: a 429 on ONDO left it at a stale 2x. That direction was
        harmless, but the same failure on a coin left at 10x would not have been.
        """
        if sym in self.lev_set:
            return True
        if not self.live:
            return True
        want = min(self.leverage, self.max_lev.get(sym, self.leverage))
        for a in range(4):
            try:
                r = self.ex.update_leverage(want, sym, is_cross=False)
                # a rejection comes back as {"status": "err"} WITHOUT raising, so the
                # old try/except alone could not see it
                if isinstance(r, dict) and r.get("status") == "ok":
                    self.lev_set.add(sym)
                    return True
                self.log(f"WARN leverage {sym} rejected: {r}")
            except Exception as e:
                self.log(f"WARN leverage {sym} try {a+1}/4: {getattr(e, 'code', None) or e}")
            time.sleep(min(15.0, 2.0 ** a))
        return False

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

    def _cancel_stray(self, sym):
        """Cancel every open order this account has in `sym`.

        Belt to the braces of cancelling a known oid: after a phantom fill the bot may not
        hold the right id, and an order left resting is unmanaged exposure. Only ever
        called for a symbol the bot has just decided it holds no position in.
        """
        if not self.live:
            return
        try:
            for o in self.info.open_orders(self.address):
                if o.get("coin") == sym:
                    self.log(f"  cancelling stray {sym} order {o['oid']} "
                             f"({o.get('side')} sz={o.get('sz')} @ {o.get('limitPx')})")
                    self.ex.cancel(sym, o["oid"])
        except Exception as e:
            self.log(f"WARN stray-cancel {sym}: {e}")

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
        except Exception as e:
            self.log(f"WARN query oid {sym}/{oid}: {e}")
            return (0.0, None)
        # FAIL CLOSED. The exchange answers {"status": "unknownOid"} for an order it has
        # not indexed yet, and there is no order object in that reply. The old code did
        #     rem = float(st["order"]["order"].get("sz", 0))  ->  0 remaining
        #     filled = want_sz - rem                          ->  a FULL FILL
        # so any unreadable reply invented a position that never existed. That is what
        # happened to MET on 2026-07-30: promoted 1.2s after placing, reconciled away 14
        # minutes later as "vanished on exchange", with the real order still resting.
        # Anything we cannot positively read as filled must be reported as UNFILLED.
        if not isinstance(st, dict) or st.get("status") != "order":
            self.log(f"WARN oid {sym}/{oid} unresolvable "
                     f"({(st or {}).get('status')!r}) — treating as unfilled")
            return (0.0, None)
        inner = st.get("order") or {}
        o = inner.get("order") or {}
        if "sz" not in o:
            self.log(f"WARN oid {sym}/{oid} reply has no size — treating as unfilled")
            return (0.0, None)
        try:
            rem = float(o["sz"])                        # sz is the REMAINING size
        except (TypeError, ValueError):
            self.log(f"WARN oid {sym}/{oid} unparseable size {o.get('sz')!r}")
            return (0.0, None)
        if inner.get("status") == "filled":
            return (want_sz, None)
        return (max(0.0, want_sz - rem), None)

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

    # ---------- queue position (ported from provision_bot.py / mexc_api.py) ----------
    def book(self, coin):
        """Full L2 book in ONE call: (bid, ask, bids, asks) as [(px, sz), ...].

        paper_bot.best_bid_ask() makes this same l2Book call and discards everything
        but the touch, so queue depth costs no extra API calls.
        """
        b = paper_bot.hl_post({"type": "l2Book", "coin": coin})
        lv = b.get("levels")
        if not lv or len(lv) < 2 or not lv[0] or not lv[1]:
            return None
        bids = [(float(x["px"]), float(x["sz"])) for x in lv[0]]
        asks = [(float(x["px"]), float(x["sz"])) for x in lv[1]]
        return (bids[0][0], asks[0][0], bids, asks)

    @staticmethod
    def queue_ahead_usd(levels, px, is_sell):
        """USD resting at or better than our price -- what must be consumed before we fill.

        Ported from mexc_api.queue_ahead_usd, which provision_bot.py uses to answer the
        question every offline fill study here has been unable to: a trade printing at
        our price proves someone traded, not that WE were at the front of the queue.

        Better-priced orders are hit first; orders AT our price were queued before us, so
        both count. Deliberately conservative in the same way as the original: it ignores
        cancellations (which help us) and new orders joining ahead (which hurt us).
        """
        tot = 0.0
        for lpx, lsz in levels:
            if (lpx <= px) if is_sell else (lpx >= px):
                tot += lpx * lsz
        return tot

    # ---------- flow toxicity (SHADOW ONLY -- logged, never acted on) ----------
    def load_tape_flow(self):
        """Read the tail of today's tape into per-coin, per-minute signed notional.

        Once per cycle, not per signal -- the file is ~900k rows/day and this is disk
        work inside the trading loop. Must never raise: an unavailable tape means the
        toxicity columns are blank, not that trading stops.
        """
        self.flow = {}
        try:
            day = datetime.now(timezone.utc).strftime("%Y%m%d")
            path = os.path.join(self.tape_dir, f"tape_{day}.csv")
            if not os.path.exists(path):
                return
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                if size > TAPE_TAIL_MB * 1024 * 1024:
                    f.seek(size - TAPE_TAIL_MB * 1024 * 1024)
                    f.readline()                       # discard the partial line
                data = f.read().decode("utf-8", "replace")
            cutoff = now_ms() - (FLOW_MINS + 5) * 60 * 1000
            n = 0
            for line in data.splitlines():
                p = line.split(",")
                if len(p) < 5:
                    continue
                try:
                    t = int(p[0])
                    if t < cutoff:
                        continue
                    ntl = float(p[3]) * float(p[4])
                except ValueError:
                    continue
                d = self.flow.setdefault(p[1], {})
                b = d.setdefault(t // 60000, [0.0, 0.0])
                if p[2] == "B": b[0] += ntl
                else:           b[1] += ntl
                n += 1
            self.log(f"tape flow: {n:,} prints, {len(self.flow)} coins "
                     f"(last {FLOW_MINS}m, shadow only)")
        except Exception as e:
            self.flow = {}
            self.log(f"WARN tape flow: {e}")

    def toxicity(self, sym, brk):
        """(vpin30, vpin60, adverse_ofi) or Nones. See analysis/toxicity.py.

        vpin  = sum|buy-sell| / sum(buy+sell) over trailing minutes. Unsigned toxicity.
        adverse_ofi = trailing OFI x breakout direction. We fade, so flow continuing in
                      the breakout direction is flow running INTO our resting order.
        """
        try:
            d = self.flow.get(sym)
            if not d:
                return (None, None, None)
            now_m = now_ms() // 60000

            def agg(mins):
                num = den = signed = 0.0
                seen = 0
                for m in range(now_m - mins + 1, now_m + 1):
                    v = d.get(m)
                    if not v:
                        continue
                    num += abs(v[0] - v[1]); den += v[0] + v[1]
                    signed += v[0] - v[1]; seen += 1
                if den <= 0 or seen < max(3, mins // 6):
                    return (None, None)
                return (num / den, signed / den)

            v30, _ = agg(30)
            v60, ofi = agg(FLOW_MINS)
            return (v30, v60, None if ofi is None else ofi * brk)
        except Exception:
            return (None, None, None)

    # ---------- entry ----------
    def open_pos(self, sym, brk, feat):
        """Place a post-only entry. Does NOT create a position — manage_pending() does,
        and only if the order actually fills inside ENTRY_WINDOW_S."""
        if sym in self.pending or sym in self.positions or sym in self.exiting:
            return
        if len(self.positions) + len(self.pending) >= self.max_positions:
            # this used to return silently, so capacity-driven skips were invisible --
            # 31 signals detected vs 27 placed with no record of why
            self.n_capped += 1
            self.log(f"SKIP {sym}: position cap ({self.max_positions}) full")
            return
        # Per-side cap: a runaway backstop, not a capacity limit. Signals arrive in
        # correlated bursts -- one market-wide move filled every slot on every arm with
        # LONGs, and three same-direction entries once fired 38s apart -- so a total
        # position count does not bound directional exposure. Counts resting orders too,
        # or a burst could blow through it before any of them fill.
        side_dir = 1 if brk < 0 else -1              # fade: down-break -> LONG
        n_side = sum(1 for p in list(self.positions.values()) + list(self.pending.values())
                     if p.get("dir") == side_dir)
        if n_side >= self.max_per_side:
            self.n_capped += 1
            self.log(f"SKIP {sym}: per-side cap ({self.max_per_side} "
                     f"{'LONG' if side_dir > 0 else 'SHORT'}) full")
            return
        gross = sum(p["notional"] for p in self.positions.values())
        mult = self.size_mult(feat)
        notional = self.notional * mult
        if gross + notional > self.max_gross:
            self.n_capped += 1
            self.log(f"SKIP {sym}: gross cap (${gross:.0f} + ${notional:.0f} > ${self.max_gross:.0f})")
            return
        if self.day_pnl <= -abs(self.daily_loss_limit):
            self.log(f"SKIP {sym}: daily loss limit hit (${self.day_pnl:+.2f})")
            return
        bk = self.book(sym)
        if bk is None:
            return
        bid, ask, bids, asks = bk
        is_buy = brk < 0                        # fade: down-breakout -> LONG
        sd = self.sz_dec.get(sym, 2)
        mid = 0.5 * (bid + ask)
        spread_bps = ((ask - bid) / mid * 1e4) if mid > 0 else None
        # CHEAP-SPREAD CROSSING (analysis/spread_gate.py). Crossing costs the spread, so
        # when the spread is small, buying a certain fill is worth it: ~26% of resting
        # entries never fill, and those are the ones that moved in our favour immediately
        # (+$0.313/trade net of spread AND taker fee, t=+2.7). When the spread is wide the
        # cost exceeds what those misses are worth, and resting is right.
        cross = (CROSS_SPREAD_BPS > 0 and spread_bps is not None
                 and spread_bps <= CROSS_SPREAD_BPS)
        # maker rests at the NEAR touch; taker takes the FAR side
        near = bid if is_buy else ask
        far = ask if is_buy else bid
        px = round_px(far if cross else near, sd, is_buy)
        sz = round_sz(notional / max(px, 1e-12), sd)
        if px <= 0 or sz <= 0 or sz * px < MIN_NOTIONAL:
            self.log(f"SKIP {sym}: unrepresentable or sub-minimum "
                     f"(px={px} sz={sz} ntl=${sz*px:.2f} < ${MIN_NOTIONAL})")
            return
        if not self._set_leverage(sym):
            # skipping one signal is cheap; an unknown-leverage no-stop position is not
            self.log(f"SKIP {sym}: could not confirm {self.leverage}x isolated leverage")
            return
        tox = self.toxicity(sym, brk)          # shadow only: recorded, never gates
        # queue ahead of us at the price we rest at. Meaningless when crossing (we are the
        # aggressor), so record it only for maker placements.
        q_usd = None if cross else self.queue_ahead_usd(
            asks if not is_buy else bids, px, not is_buy)
        base = {
            "is_buy": is_buy, "px": px, "sz": sz, "placed_ms": now_ms(),
            "dir": 1 if is_buy else -1, "notional": notional, "mult": mult,
            "prior_h": feat["prior_h"], "prior_l": feat["prior_l"],
            "entry_bid": bid, "entry_ask": ask, "vratio": feat["vratio"],
            "rv": feat["rv"], "ats_ratio": feat.get("ats_ratio"), "tox": tox,
            "queue_usd": q_usd, "spread_bps": spread_bps, "crossed": int(cross),
            "tier": self.universe.get(sym),
            "queue_ratio": (q_usd / notional) if (q_usd is not None and notional) else None,
        }

        if cross:
            # IOC with a slippage cap. Our size is far below the depth at the touch
            # ($25-75 against $500-1,100 measured), so this fills at the touch without
            # walking the book -- but the cap bounds the damage if the book moves.
            s = CROSS_CAP_BPS / 1e4
            cap = round_px(px * (1 + s) if is_buy else px * (1 - s), sd, not is_buy)
            oid, fsz, fpx = self._place(sym, is_buy, sz, cap, "Ioc")
            if fsz <= 0:
                self.log(f"MISS  {sym:12s} crossing IOC got no fill @ cap {cap:.6g} "
                         f"(spread was {spread_bps:.1f}b) — signal abandoned")
                return
            pend = dict(base, oid=oid, filled=fsz, px=fpx or px)
            self.log(f"CROSS {sym:12s} {'BUY ' if is_buy else 'SELL':4s} sz={fsz} "
                     f"@ {fpx or px:.6g}  (vr={feat['vratio']:.1f}x size={mult:.1f}x "
                     f"${notional:.0f})  spread={spread_bps:.1f}b <= {CROSS_SPREAD_BPS}b "
                     f"— took the far side for a certain fill")
            self._promote(sym, pend, round_sz(fsz, sd))
            return

        oid, fsz, fpx = self._place(sym, is_buy, sz, px, "Alo")
        if oid is None and fsz == 0:
            return
        self.pending[sym] = dict(base, oid=oid, filled=fsz)
        self.log(f"PLACE {sym:12s} {'BUY ' if is_buy else 'SELL':4s} sz={sz} @ {px:.6g}  "
                 f"(vr={feat['vratio']:.1f}x rv={feat['rv']:.4f} size={mult:.1f}x "
                 f"${notional:.0f})  queue=${q_usd:,.0f} ({q_usd/notional:.1f}x ours) "
                 f"spread={spread_bps:.1f}b  resting up to {ENTRY_WINDOW_S}s")
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
            "tox": pend.get("tox", (None, None, None)),
            "queue_usd": pend.get("queue_usd"), "queue_ratio": pend.get("queue_ratio"),
            "spread_bps": pend.get("spread_bps"), "crossed": pend.get("crossed", 0),
            "tier": pend.get("tier"), "ats_ratio": pend.get("ats_ratio"),
            # kept so reconcile can cancel the entry order if this turns out to be a
            # phantom fill rather than a real position
            "oid": pend.get("oid"),
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
                                    "" if pend["ats_ratio"] is None else f"{pend['ats_ratio']:.2f}",
                                    *[("" if x is None else f"{x:.4f}")
                                      for x in pend.get("tox", (None, None, None))],
                                    *self._q3(pend)])
        q = pend.get("queue_ratio")
        self.log(f"MISS  {sym:12s} unfilled after {rested}s @ {pend['px']:.6g} — "
                 f"signal abandoned (queue was {q:.1f}x ours)" if q is not None else
                 f"MISS  {sym:12s} unfilled after {rested}s @ {pend['px']:.6g} — abandoned")
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
                             "px": None, "repegs": 0, "done": 0.0, "order_sz": 0.0,
                             "order_ms": 0}
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
        ex["order_ms"] = now_ms()
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
                int(taker), ex.get("repegs", 0), sz,
                *[("" if x is None else f"{x:.4f}")
                  for x in p.get("tox", (None, None, None))],
                *self._q3(p),
                ("" if p.get("ats_ratio") is None else f"{p['ats_ratio']:.4f}")])
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
            age = (now_ms() - pend["placed_ms"]) / 1000
            if age < FILL_QUERY_MIN_S:
                # too young to have been indexed; querying now is what produced the
                # unknownOid race. Nothing is lost by waiting one poll.
                continue
            filled, _ = self._filled_sz(sym, pend["oid"], pend["sz"])
            filled = max(filled, pend["filled"])
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
                if (now_ms() - ex.get("order_ms", 0)) / 1000 < FILL_QUERY_MIN_S:
                    continue                     # same indexing race as entries
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
                ex = self.exiting.pop(sym, None)
                # The position may be a phantom from a mis-read fill, in which case the
                # ENTRY order is very likely still resting on the book. Cancel anything
                # we still have an id for, or it lingers unmanaged (observed with MET).
                for oid in (p.get("oid"), (ex or {}).get("oid")):
                    if oid not in (None, -1):
                        self.log(f"RECONCILE {sym}: cancelling still-resting order {oid}")
                        self._cancel(sym, oid)
                self._cancel_stray(sym)
                self.log(f"RECONCILE {sym}: gone on exchange (liquidated, closed "
                         f"externally, or never actually filled) — dropping local "
                         f"position, P&L NOT booked")
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
        self.load_tape_flow()          # shadow toxicity features for this cycle's signals
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
        self.n_capped = 0
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
        cap = f", {self.n_capped} capped" if self.n_capped else ""
        self.log(f"cycle done: {n_sig} signals, {len(self.positions)} open, "
                 f"{len(self.pending)} resting{cap}, cum=${self.cum_pnl:+.2f}, "
                 f"day=${self.day_pnl:+.2f}" + (f", equity=${eq:,.2f}" if eq else ""))

    def run(self):
        mode = "LIVE — REAL MONEY" if self.live else "DRY RUN"
        xs = (f"cross<={CROSS_SPREAD_BPS:g}b" if CROSS_SPREAD_BPS > 0 else "cross=off")
        szs = (f"x({SIZE_MIN:g}-{SIZE_MAX:g} ats)" if self.size_by_ats else "FLAT")
        self.log(f"=== live bot [15m-ats] {mode} | notional=${self.notional} {szs} "
                 f"lev={self.leverage}x isolated | {xs} entry_window={ENTRY_WINDOW_S}s "
                 f"exit_grace={EXIT_GRACE_S}s | caps: {self.max_positions}pos "
                 f"{self.max_per_side}/side ${self.max_gross:.0f}gross "
                 f"${self.daily_loss_limit:.0f}daily-loss ===")
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
    ap.add_argument("--max-positions", type=int, default=10,
                    help="total concurrent positions+resting orders. Set to 2x "
                         "--max-per-side to make the per-side caps the binding limit.")
    ap.add_argument("--max-per-side", type=int, default=20,
                    help="max concurrent LONG or SHORT positions (default 20). A runaway "
                         "backstop, not a capacity limit: signals arrive in correlated "
                         "bursts, so a total count does not bound directional exposure.")
    ap.add_argument("--daily-loss-limit", type=float, default=50.0,
                    help="stop opening new positions once realized P&L today is below -X")
    ap.add_argument("--leverage", type=int, default=3, help="isolated leverage")
    ap.add_argument("--flat-size", action="store_true",
                    help="disable ats sizing (flat notional). The tape favours this; the "
                         "arm's own 57 trades favour ats. Unresolved -- see analysis/sizing_ab.py")
    ap.add_argument("--cross-spread-bps", type=float, default=None,
                    help=f"cross the spread instead of resting when the spread is at or "
                         f"below this many bps (default {CROSS_SPREAD_BPS}; 0 = always "
                         f"rest, the pre-2026-07-27 behaviour). See analysis/spread_gate.py")
    ap.add_argument("--tape-dir", default=None,
                    help=f"tape logger output dir, for shadow flow-toxicity logging "
                         f"(default {TAPE_DIR}). Features are recorded, never acted on.")
    a = ap.parse_args()
    if a.cross_spread_bps is not None:
        CROSS_SPREAD_BPS = a.cross_spread_bps
    LiveBot(a.datadir, live=a.live, notional=a.notional, max_gross=a.max_gross,
            max_positions=a.max_positions, daily_loss_limit=a.daily_loss_limit,
            leverage=a.leverage, size_by_ats=not a.flat_size, tape_dir=a.tape_dir,
            max_per_side=a.max_per_side).run()
