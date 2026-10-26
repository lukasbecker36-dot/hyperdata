#!/usr/bin/env python3
"""
LIVE execution engine for the MEXC spot dip-provision strategy.

Strategy (validated: 18 months / 219 tokens backtest, plus a paired spot-vs-perp
revalidation and ~9 days of live paper):
  universe  crypto USDT spot pairs whose listing age is Day 3-45
  entry     MARKET BUY when price trades 5% below the prior hourly close  (TAKER)
  exit      resting LIMIT SELL at +10% from the fill                      (MAKER, 0 fee)
  backstop  MARKET SELL after 72h. No stop loss -- swept and rejected twice.
  size      $LOT per lot, at most 5 lots per coin, so cash is bounded per coin

Taker entry is deliberate. Live paper showed a resting bid is severely adversely
selected on spot: the fills a maker actually gets earned +$1.52 against +$5.09 for the
ones it misses, and were stopped out 2.5x more often, because a passive bid only fills
when enough selling arrives to clear the whole book above it -- a genuine collapse.
Crossing costs ~5bp against a measured 4.25% slippage budget.

DIFFERENCES FROM THE PAPER BOT, all forced by reality rather than preference:
  * The paper bot detects fills retrospectively from an hourly bar's low. Live, the
    trigger has to be watched continuously, so this polls all prices every few seconds
    (one request for every symbol) and crosses the spread the moment a level breaks.
  * The +10% exit is a REAL resting order, so MEXC fills it while we sleep. We only
    poll to learn that it happened.
  * Quantity is never assumed. MEXC charges the buy fee in the BASE asset, so the
    credited amount is under executedQty; the exit is always sized from the actual
    free balance.

SHARING AN ACCOUNT WITH basis-trade
/opt/basis-trade trades MEXC spot on the same credentials. Two protections:
  * At startup every asset already carrying a balance is recorded as FOREIGN and is
    never traded or sold by this engine.
  * This engine only ever sells quantities it has itself recorded buying, and never
    more than its own ledger says it owns.
A separate MEXC sub-account is still the right answer; these guards are a seatbelt,
not a substitute.

SAFETY
  * --dry-run is the DEFAULT. Orders are logged, never sent. --live is explicit.
  * A file named HALT in the data directory stops all new entries immediately;
    existing positions still exit normally.
  * MAX_EXPOSURE_USD caps total open cost basis.
  * AmbiguousOrderError (timeout / 5xx / unparseable) is never retried blindly -- the
    order is resolved by its client id, because it may already exist.

    python3 provision_exec.py --datadir ./live_provision --lot 2 --dry-run
    python3 provision_exec.py --datadir ./live_provision --lot 2 --live
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import mexc_spot_api as universe_api
import mexc_trade
from mexc_trade import AmbiguousOrderError, ExchangeError, MexcSpot

try:
    import telegram_notify as tg
except Exception:                            # noqa: BLE001
    tg = None

DIP = 0.05
TP = 0.10
MAX_HOLD_H = 72
MAX_LOTS_PER_COIN = 5
FROM_DAY, TO_DAY = 3, 45
SCAN_SECS = 8                # price-poll cadence (one request covers every symbol)
BAR_SECS = 3600
CYCLE_OFFSET = 20
MAX_EXPOSURE_USD = 2000.0    # hard ceiling on total open cost basis
RECONCILE_EVERY_S = 900
SUMMARY_EVERY_H = 6          # Telegram digest cadence, on UTC boundaries

# ---- pre-committed stopping rule, fixed 2026-08-17, BEFORE the outcome is known ----
# Recorded here rather than decided while watching, because the failure mode of a live
# test is drifting: checking daily, feeling worse, and tuning parameters mid-experiment.
#
# Evidence at the time of writing: 294 closed lots, realised +$14.21, unrealised
# -$44.82, net -$30.62 on ~$204 at risk. The target-hit rate (66%) matches the
# backtest's 69%, so entries are fine; loser depth does NOT -- -34.5% against a modelled
# -17.1%, stable across 50 losing exits, which alone makes expectancy about -$0.10/lot.
# Every candidate fix has been tested and rejected: stops (twice), volatility
# normalisation (twice), BTC and coin-MA regime filters, coin selection on past P&L, and
# a learned per-lot filter (live AUC 0.49).
#
# A -$30 drawdown over 294 lots sits between the 5th percentile and the worst on record
# for that many lots, so it is possible but poor. 500 closed lots takes the losing-exit
# sample to ~100, which is enough to separate "this cohort is different" from "a bad
# fortnight". -$50 is the point at which continuing costs more than the answer is worth.
#
# On trigger: entries HALT. Open positions still exit on target or at the 72h backstop.
# Nothing is force-sold, because crystallising the open book would throw away exactly the
# recovery the test is trying to measure.
STOP_NET_USD = -50.0         # halt if realised + unrealised falls to this
STOP_CLOSED = 500            # halt once this many lots have closed

# ---- collective-volatility monitor / re-entry condition ----
# The strategy's edge degrades monotonically with the volatility of the whole new-listing
# cohort: loser depth runs -0.271 per lot in the calmest quartile against -0.420 in the
# wildest, while the hit rate barely moves. Live ran at ~4.0%/h against a historical
# median of 1.94%, i.e. roughly 2x, for every one of its 115 hours -- the last day below
# 2% was 2026-06-29, seven weeks before the run started.
#
# So this is measured hourly and reported, and crossing back under CV_RESUME_PCT is the
# condition for reallocating to the strategy. It deliberately does NOT auto-resume:
# restarting real-money trading unattended on a single crossing is a worse failure mode
# than missing a day, and one clean hour under the line is not a regime change.
CV_RESUME_PCT = 0.020        # collective hourly sigma below which conditions are normal
CV_LOOKBACK_BARS = 100       # bars fetched per symbol to measure trailing 72h sigma
MIN_SELL_MARGIN = 1.25       # bundle up to this multiple of the exchange minimum, so
                             # a tick down between valuing and selling cannot push the
                             # order under the limit and get it rejected
MAX_BELOW_LEVEL = 0.10       # refuse an entry priced this far BELOW the trigger.
                             # The backtest fills AT the level; live, a coin can
                             # already be far through it when the hourly refresh runs
                             # or when a candle gaps, and buying 40% under the trigger
                             # is a different trade from the one that was validated.
                             # 10% bounds the deviation without rejecting normal wicks.


def _hourly_sigma(closes, lookback=72):
    """
    Trailing hourly realised volatility from a close series. None if too short.

    Stdlib only, so this is a plain two-pass computation rather than numpy.
    """
    c = [float(x) for x in closes if x and float(x) > 0]
    if len(c) < 25:
        return None
    import math
    rets = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))][-lookback:]
    if len(rets) < 24:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return var ** 0.5


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def now_ms():
    return int(time.time() * 1000)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Executor:
    def __init__(self, datadir, lot_usd, live=False, label=None):
        self.datadir = datadir
        os.makedirs(datadir, exist_ok=True)
        self.lot = Decimal(str(lot_usd))
        self.live = live
        self.label = label or ("provision-LIVE" if live else "provision-DRY")
        self.cli = MexcSpot()
        self.state_file = os.path.join(datadir, "state.json")
        self.trade_csv = os.path.join(datadir, "trades.csv")
        self.log_file = os.path.join(datadir, "exec.log")
        self.halt_file = os.path.join(datadir, "HALT")
        self.flatten_file = os.path.join(datadir, "FLATTEN")

        self.levels = {}        # symbol -> {"level": Decimal, "bar_ts": int}
        self.lots = []          # open positions
        self.foreign = set()    # assets owned by something else (basis-trade)
        self.cum_pnl = 0.0
        self.n_closed = 0
        self.n_win = 0
        self.next_id = 1
        self._last_bar = None
        self._last_recon = 0
        self._last_summary_slot = None
        self.collective_vol = None      # median trailing sigma of the cohort
        self.cv_history = []            # recent hourly readings, for range/trend
        self._cv_alerted = False        # so the all-clear fires once, not hourly
        self._load()
        self._init_csv()

    # ---------------- infrastructure ----------------
    def log(self, msg):
        line = f"{iso(now_ms())}  {msg}"
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            # A non-UTF-8 console must never be able to kill a trading loop.
            print(line.encode("ascii", "replace").decode(), flush=True)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:                    # noqa: BLE001
            pass

    def notify(self, msg):
        if tg is None or not tg.enabled():
            return
        try:
            tg.send(f"<b>[{self.label}]</b> {msg}")
        except Exception:                    # noqa: BLE001
            pass

    def _init_csv(self):
        if not os.path.exists(self.trade_csv):
            with open(self.trade_csv, "w", newline="") as f:
                csv.writer(f).writerow([
                    "close_time", "symbol", "entry_time", "entry_px", "exit_px",
                    "qty", "cost_usd", "proceeds_usd", "pnl_usd", "ret_pct",
                    "hold_h", "reason", "buy_client_id", "sell_client_id", "cum_pnl"])

    def _save(self):
        tmp = self.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"lots": self.lots, "levels":
                       {k: {"level": str(v["level"]), "bar_ts": v["bar_ts"]}
                        for k, v in self.levels.items()},
                       "cum_pnl": self.cum_pnl, "n_closed": self.n_closed,
                       "n_win": self.n_win, "next_id": self.next_id,
                       "foreign": sorted(self.foreign),
                       "last_summary_slot": self._last_summary_slot,
                       "collective_vol": self.collective_vol,
                       "cv_history": self.cv_history[-48:],
                       "cv_alerted": self._cv_alerted,
                       "last_bar": self._last_bar}, f)
        os.replace(tmp, self.state_file)

    def _load(self):
        if not os.path.exists(self.state_file):
            return
        try:
            s = json.load(open(self.state_file))
            self.lots = s.get("lots", [])
            self.levels = {k: {"level": Decimal(v["level"]), "bar_ts": v["bar_ts"]}
                           for k, v in (s.get("levels") or {}).items()}
            self.cum_pnl = s.get("cum_pnl", 0.0)
            self.n_closed = s.get("n_closed", 0)
            self.n_win = s.get("n_win", 0)
            self.next_id = s.get("next_id", 1)
            self.foreign = set(s.get("foreign", []))
            self._last_summary_slot = s.get("last_summary_slot")
            self.collective_vol = s.get("collective_vol")
            self.cv_history = list(s.get("cv_history") or [])
            self._cv_alerted = bool(s.get("cv_alerted", False))
            self._last_bar = s.get("last_bar")
        except Exception as e:               # noqa: BLE001
            self.log(f"WARN could not load state: {e}")

    def halted(self):
        return os.path.exists(self.halt_file)

    def exposure(self):
        return sum(float(l["cost_usd"]) for l in self.lots)

    # ---------------- reconciliation ----------------
    def reconcile(self, first=False):
        """
        Make local belief match the exchange. On the first pass, every asset already
        holding a balance is marked FOREIGN so this engine never touches inventory
        belonging to basis-trade (or to a human).
        """
        if not self.cli.authenticated:
            if first:
                self.log("no credentials: running in DRY mode with no account access")
            return
        try:
            self.cli.sync_time()
            balances = self.cli.balances()
            open_orders = {o.client_id: o for o in self.cli.open_orders()}
        except ExchangeError as e:
            self.log(f"WARN reconcile failed: {e}")
            return

        if first:
            ours = {l["base"] for l in self.lots}
            self.foreign = {a for a in balances if a not in ours and a != "USDT"}
            if self.foreign:
                self.log(f"marked {len(self.foreign)} pre-existing assets as FOREIGN "
                         f"(never traded here): {sorted(self.foreign)[:10]}")

        # resolve every lot's resting sell against the venue
        for lot in list(self.lots):
            cid = lot.get("sell_client_id")
            if not cid:
                continue
            if cid in open_orders:
                continue                     # still resting, as expected
            try:
                o = self.cli.get_order_by_client_id(lot["symbol"], cid)
            except ExchangeError as e:
                self.log(f"WARN lookup {lot['symbol']} {cid}: {e}")
                continue
            if o is None:
                lot["sell_client_id"] = None  # never landed; manage() will re-place
                self.log(f"RECON {lot['symbol']}: sell order never landed, will re-place")
            elif o.is_filled:
                self._book_exit(lot, o.avg_price, o.executed_qty, "target")
            elif not o.is_open:
                lot["sell_client_id"] = None
                self.log(f"RECON {lot['symbol']}: sell {o.status}, will re-place")
        self._adopt_untracked(balances)
        self._save()

    def _adopt_untracked(self, balances):
        """
        Take ownership of any holding the ledger cannot account for.

        The exchange is the source of truth. If we hold a non-FOREIGN base asset that
        no lot covers, something filled without being recorded -- an async ack we gave
        up on, a crash between the buy and the save, a partial. Rather than leave it
        unmanaged forever, reconstruct the entry from myTrades and adopt it so the
        normal +10% exit and 72h backstop apply.
        """
        info = self.cli.exchange_info()
        tracked = {}
        for l in self.lots:
            tracked[l["base"]] = tracked.get(l["base"], Decimal(0)) + Decimal(l["qty"])
        for asset, (free, locked) in balances.items():
            if asset == "USDT" or asset in self.foreign:
                continue
            have = free + locked
            gap = have - tracked.get(asset, Decimal(0))
            symbol = f"{asset}USDT"
            if symbol not in info:
                continue
            if gap <= 0 or info[symbol].round_qty(gap) <= 0:
                continue
            entry_px, entry_ms = self._entry_from_trades(symbol, gap)
            if entry_px is None:
                self.log(f"ADOPT {symbol}: {gap} untracked but no buy found in "
                         f"myTrades -- leaving alone, inspect manually")
                continue
            lot = {"id": self.next_id, "symbol": symbol, "base": asset,
                   "entry_px": str(entry_px), "qty": str(gap),
                   "cost_usd": float(entry_px * gap), "entry_ms": entry_ms,
                   "buy_client_id": "adopted", "sell_client_id": None}
            self.next_id += 1
            self.lots.append(lot)
            age_h = (now_ms() - entry_ms) / 3600000.0
            self.log(f"ADOPT {symbol}: {gap} @ {entry_px} (${lot['cost_usd']:.2f}, "
                     f"{age_h:.1f}h old) -- now managed")
            self.notify(f"\U0001F527 adopted untracked {asset}: {gap} @ {entry_px} "
                        f"(${lot['cost_usd']:.2f})")
            if self.live:
                self.place_exit(lot)

    def _entry_from_trades(self, symbol, qty):
        """Qty-weighted entry price and earliest time for `qty`, from recent buys."""
        try:
            trades = self.cli.my_trades(symbol, limit=100)
        except ExchangeError as e:
            self.log(f"WARN myTrades {symbol}: {e}")
            return None, now_ms()
        buys = sorted((t for t in trades if t.get("isBuyer")),
                      key=lambda t: int(t.get("time", 0)), reverse=True)
        remaining, cost, used, earliest = Decimal(qty), Decimal(0), Decimal(0), None
        for t in buys:
            q = mexc_trade._dec(t.get("qty"))
            if q <= 0:
                continue
            take = min(q, remaining)
            if take <= 0:
                break
            cost += take * mexc_trade._dec(t.get("price"))
            used += take
            earliest = int(t.get("time", 0)) or earliest
            remaining -= take
            if remaining <= 0:
                break
        if used <= 0:
            return None, now_ms()
        return cost / used, earliest or now_ms()

    # ---------------- hourly: refresh trigger levels ----------------
    def refresh_levels(self):
        bar_now = int(time.time()) // BAR_SECS * BAR_SECS
        closed = bar_now - BAR_SECS
        try:
            uni = universe_api.new_listings(FROM_DAY, TO_DAY)
        except Exception as e:               # noqa: BLE001
            self.log(f"WARN universe: {e}")
            return
        info = self.cli.exchange_info()
        levels = {}
        sigmas = []
        for sym, meta in uni.items():
            if meta["base"] in self.foreign:
                continue                     # someone else owns this asset
            if sym not in info:
                continue
            try:
                k = universe_api.klines(
                    sym, bar_now - CV_LOOKBACK_BARS * BAR_SECS, bar_now)
            except Exception:                # noqa: BLE001
                continue
            bar = next((c for c in k if c[0] == closed), None)
            if bar is None or bar[4] <= 0:
                continue
            levels[sym] = {"level": Decimal(str(bar[4])) * Decimal(str(1 - DIP)),
                           "bar_ts": bar_now}
            sig = _hourly_sigma([c[4] for c in k])
            if sig is not None:
                sigmas.append(sig)
            time.sleep(universe_api.REQUEST_SLEEP)
        self.levels = levels
        self._last_bar = bar_now
        self._update_collective_vol(sigmas)
        self.log(f"levels refreshed for {len(levels)} symbols "
                 f"(bar {iso(bar_now*1000)}), exposure ${self.exposure():.2f}")
        self._save()

    # ---------------- fast loop: trigger and buy ----------------
    def scan(self):
        if self.halted() or not self.levels:
            return
        try:
            px = self.cli.prices()
        except ExchangeError as e:
            self.log(f"WARN price poll: {e}")
            return
        for sym, lv in list(self.levels.items()):
            p = px.get(sym)
            if p is None or p <= 0 or p > lv["level"]:
                continue
            if p < lv["level"] * Decimal(str(1 - MAX_BELOW_LEVEL)):
                self.log(f"SKIP {sym}: {float(p/lv['level']-1)*100:.1f}% below the "
                         f"trigger, past the {MAX_BELOW_LEVEL:.0%} guard")
                self.levels.pop(sym, None)
                continue
            n_here = sum(1 for l in self.lots if l["symbol"] == sym)
            if n_here >= MAX_LOTS_PER_COIN:
                continue
            if self.exposure() + float(self.lot) > MAX_EXPOSURE_USD:
                self.log(f"SKIP {sym}: exposure cap ${MAX_EXPOSURE_USD:.0f} reached")
                continue
            self.enter(sym, p)
            # one lot per level break; the next hourly refresh resets the trigger
            self.levels.pop(sym, None)

    _TERMINAL = ("FILLED", "CANCELED", "EXPIRED", "REJECTED")

    def _settle(self, symbol, cid, order, tries=8, delay=1.0):
        """
        Wait for a MARKET order to actually settle.

        MEXC acknowledges a market order with status NEW and executedQty 0, then
        matches asynchronously. Reading that first response as "no fill" abandons a
        position that really exists -- which is exactly what happened on the first
        two live orders (HOODRAT, FRONG), leaving unmanaged inventory with no exit.
        Poll by client id until the order reaches a terminal state or reports a fill.
        """
        if order.executed_qty > 0 or order.status in self._TERMINAL:
            return order
        for _ in range(tries):
            time.sleep(delay)
            try:
                o = self.cli.get_order_by_client_id(symbol, cid)
            except ExchangeError:
                continue
            if o is None:
                return order
            if o.executed_qty > 0 or o.status in self._TERMINAL:
                return o
        self.log(f"WARN {symbol} {cid} still unsettled after {tries}s; "
                 f"reconcile will adopt it if it fills")
        return order

    def enter(self, symbol, price):
        cid = f"pvb{self.next_id}-{uuid.uuid4().hex[:8]}"
        if not self.live:
            self.log(f"DRY  BUY  {symbol:14} ${self.lot} @ ~{price}  (cid {cid})")
            self.next_id += 1
            self._save()
            return
        try:
            o = self.cli.market_buy_quote(symbol, self.lot, client_id=cid)
        except AmbiguousOrderError as e:
            # The order may exist. Never retry -- look it up.
            self.log(f"AMBIGUOUS buy {symbol} ({e}); resolving by client id")
            time.sleep(2.0)
            try:
                o = self.cli.get_order_by_client_id(symbol, cid)
            except ExchangeError as e2:
                self.log(f"ERROR could not resolve {symbol} {cid}: {e2} -- HALTING")
                self._halt(f"unresolved order {symbol} {cid}")
                return
            if o is None:
                self.log(f"resolved: {symbol} {cid} never landed")
                return
        except ExchangeError as e:
            self.log(f"ERROR buy {symbol}: {e}")
            return
        o = self._settle(symbol, cid, o)
        if o.executed_qty <= 0:
            self.log(f"buy {symbol} really did not fill ({o.status})")
            return
        lot = {"id": self.next_id, "symbol": symbol,
               "base": self.cli.exchange_info()[symbol].base,
               "entry_px": str(o.avg_price), "qty": str(o.executed_qty),
               "cost_usd": float(o.quote_qty or (o.avg_price * o.executed_qty)),
               "entry_ms": now_ms(), "buy_client_id": cid, "sell_client_id": None}
        self.next_id += 1
        self.lots.append(lot)
        self.log(f"BUY  {symbol:14} {o.executed_qty} @ {o.avg_price} "
                 f"= ${lot['cost_usd']:.2f}  lots={len(self.lots)}")
        self._save()
        self.place_exit(lot)

    def place_exit(self, lot):
        """Resting +10% sell, sized from the ACTUAL free balance."""
        symbol = lot["symbol"]
        target = Decimal(lot["entry_px"]) * Decimal(str(1 + TP))
        if not self.live:
            self.log(f"DRY  SELL {symbol:14} {lot['qty']} @ {target}")
            return
        try:
            qty = self.cli.sellable_qty(symbol, want=Decimal(lot["qty"]))
            if qty <= 0:
                self.log(f"WARN {symbol}: nothing sellable yet")
                return
            cid = f"pvs{lot['id']}-{uuid.uuid4().hex[:8]}"
            o = self.cli.limit_sell(symbol, qty, target, client_id=cid)
            lot["sell_client_id"] = cid
            lot["sell_order_id"] = o.order_id
            lot["qty"] = str(qty)
            self.log(f"SELL rest {symbol:14} {qty} @ {target}")
            self._save()
        except AmbiguousOrderError as e:
            self.log(f"AMBIGUOUS sell {symbol} ({e}); will resolve on next reconcile")
        except ExchangeError as e:
            self.log(f"ERROR sell {symbol}: {e}")

    # ---------------- position management ----------------
    def manage(self):
        for lot in list(self.lots):
            if not lot.get("sell_client_id") and self.live:
                self.place_exit(lot)
            age_h = (now_ms() - lot["entry_ms"]) / 3600000.0
            if age_h < MAX_HOLD_H:
                continue
            if lot.get("dust") and now_ms() < lot.get("retry_after", 0):
                continue          # unsellable; recheck hourly rather than every cycle
            self.exit_at_market(lot, "time_stop")

    def exit_at_market(self, lot, reason):
        """
        Close a lot at market, bundling siblings when the lot alone is too small.

        MEXC will not accept an order below ~$1 of quote value. A $2 lot that is down
        50% is therefore UNSELLABLE on its own and would strand as permanent dust --
        which is not hypothetical: GPUBSC had four lots worth $0.77-$0.92 with the
        oldest hours from its backstop.

        The averaging-down that causes the problem also solves it. Lots accumulate in
        the same coin, so the position is worth far more than any single lot (GPUBSC's
        five totalled $4.41). This sells the due lot bundled with the FEWEST additional
        siblings needed to clear the minimum, oldest first, so as few positions as
        possible are closed early. Proceeds are allocated pro-rata by quantity.
        """
        symbol = lot["symbol"]
        if not self.live:
            self.log(f"DRY  EXIT {symbol:14} ({reason})")
            self.lots.remove(lot)
            self._save()
            return
        info = self.cli.exchange_info().get(symbol)
        min_q = float(info.min_quote) if info else 1.0
        try:
            px = float(self.cli.price(symbol))
        except ExchangeError as e:
            self.log(f"WARN price {symbol}: {e}")
            return
        if px <= 0:
            return

        group = [lot]
        value = float(lot["qty"]) * px
        if value < min_q * MIN_SELL_MARGIN:
            siblings = sorted((l for l in self.lots
                               if l["symbol"] == symbol and l is not lot),
                              key=lambda l: l["entry_ms"])
            for sib in siblings:
                if value >= min_q * MIN_SELL_MARGIN:
                    break
                group.append(sib)
                value += float(sib["qty"]) * px
            if len(group) > 1:
                self.log(f"BUNDLE {symbol}: lot worth ${float(lot['qty'])*px:.2f} is "
                         f"below the ${min_q:.2f} minimum -- selling {len(group)} lots "
                         f"together (${value:.2f})")

        if value < min_q:
            # Even the whole holding cannot be sold. Back off rather than retry each
            # cycle, and keep it visible instead of silently dropping it.
            if not lot.get("dust"):
                self.log(f"DUST {symbol}: entire position worth ${value:.2f} < "
                         f"${min_q:.2f} minimum -- cannot be sold, will retry hourly")
                self.notify(f"⚠️ {lot['base']} is dust: whole position "
                            f"${value:.2f}, below MEXC's ${min_q:.2f} minimum. "
                            f"Not sellable until it recovers.")
            for l in group:
                l["dust"] = True
                l["retry_after"] = now_ms() + 3600 * 1000
            self._save()
            return

        self._sell_group(symbol, group, reason, primary=lot)

    def _sell_group(self, symbol, group, reason, primary=None):
        """Cancel the group's resting sells, market-sell it, book proceeds pro-rata."""
        try:
            for l in group:
                if l.get("sell_order_id"):
                    try:
                        self.cli.cancel_order(symbol, l["sell_order_id"])
                    except ExchangeError as e:
                        self.log(f"note: cancel {symbol}: {e}")
            want = sum(Decimal(l["qty"]) for l in group)
            qty = self.cli.sellable_qty(symbol, want=want)
            if qty <= 0:
                self.log(f"WARN {symbol}: nothing to sell, dropping {len(group)} lots")
                for l in group:
                    if l in self.lots:
                        self.lots.remove(l)
                self._save()
                return False
            cid = f"pvx{(primary or group[0])['id']}-{uuid.uuid4().hex[:8]}"
            o = self.cli.market_sell(symbol, qty, client_id=cid)
            o = self._settle(symbol, cid, o)
            if o.executed_qty <= 0:
                self.log(f"WARN {symbol}: market sell did not fill ({o.status})")
                return False
            # pro-rata the realised proceeds across the bundle
            total_q = sum(float(l["qty"]) for l in group)
            for l in group:
                share = float(l["qty"]) / total_q if total_q else 0.0
                self._book_exit(l, o.avg_price,
                                Decimal(str(float(o.executed_qty) * share)),
                                reason if (primary is None or l is primary)
                                else f"{reason}_bundled")
            return True
        except AmbiguousOrderError as e:
            self.log(f"AMBIGUOUS exit {symbol} ({e}); leaving for reconcile")
        except ExchangeError as e:
            self.log(f"ERROR exit {symbol}: {e}")
        return False

    def flatten_all(self, reason="panic"):
        """
        Sell EVERYTHING and stop trading. Triggered by the FLATTEN request file, which
        the Telegram monitor writes after an explicit confirmation -- the monitor holds
        no credentials, so all order placement stays in this one process.

        Sells per COIN rather than per lot, which both clears MEXC's ~$1 minimum and
        keeps it to one order per symbol. HALT is set first, so no new entry can slip
        in while the book is being closed.
        """
        self._halt(f"flatten requested ({reason})")
        by = {}
        for l in self.lots:
            by.setdefault(l["symbol"], []).append(l)
        if not by:
            self.log("FLATTEN: nothing open")
            self.notify("\U0001F6D1 FLATTEN: nothing open. Entries halted.")
            return
        self.log(f"FLATTEN: closing {len(self.lots)} lots across {len(by)} coins")
        self.notify(f"\U0001F6D1 <b>FLATTEN</b>: selling {len(self.lots)} lots "
                    f"across {len(by)} coins...")
        before = self.cum_pnl
        sold, stuck = 0, []
        for symbol, group in list(by.items()):
            if not self.live:
                self.log(f"DRY  FLATTEN {symbol} ({len(group)} lots)")
                for l in group:
                    if l in self.lots:
                        self.lots.remove(l)
                continue
            try:
                px = float(self.cli.price(symbol))
            except ExchangeError:
                px = 0.0
            info = self.cli.exchange_info().get(symbol)
            min_q = float(info.min_quote) if info else 1.0
            value = px * sum(float(l["qty"]) for l in group)
            if px <= 0 or value < min_q:
                stuck.append(f"{group[0]['base']} (${value:.2f})")
                continue
            if self._sell_group(symbol, group, reason):
                sold += len(group)
        self._save()
        realised = self.cum_pnl - before
        msg = (f"\U0001F6D1 <b>FLATTEN done</b>\nsold {sold} lots, "
               f"realised ${realised:+.2f} on this close\n"
               f"total realised ${self.cum_pnl:+.2f}\n"
               f"still open: {len(self.lots)}")
        if stuck:
            msg += ("\n⚠️ unsellable (below MEXC's minimum): "
                    + ", ".join(stuck[:8]))
        # log plain text (no markup, no emoji); keep the decorated version for Telegram
        self.log(f"FLATTEN done: sold {sold} lots, realised ${realised:+.2f} on this "
                 f"close, total ${self.cum_pnl:+.2f}, still open {len(self.lots)}"
                 + (f", UNSELLABLE: {', '.join(stuck)}" if stuck else ""))
        self.notify(msg + "\n\nEntries are HALTED. /resume to restart.")

    def _book_exit(self, lot, exit_px, qty, reason):
        cost = float(lot["cost_usd"])
        proceeds = float(Decimal(str(exit_px)) * Decimal(str(qty)))
        pnl = proceeds - cost
        ret = pnl / cost * 100 if cost else 0.0
        hold_h = (now_ms() - lot["entry_ms"]) / 3600000.0
        self.cum_pnl += pnl
        self.n_closed += 1
        self.n_win += 1 if pnl > 0 else 0
        with open(self.trade_csv, "a", newline="") as f:
            csv.writer(f).writerow([
                iso(now_ms()), lot["symbol"], iso(lot["entry_ms"]), lot["entry_px"],
                str(exit_px), str(qty), f"{cost:.4f}", f"{proceeds:.4f}",
                f"{pnl:+.4f}", f"{ret:+.2f}", f"{hold_h:.2f}", reason,
                lot.get("buy_client_id", ""), lot.get("sell_client_id", ""),
                f"{self.cum_pnl:.4f}"])
        self.log(f"CLOSE {lot['symbol']:14} {reason:9} ${pnl:+.3f} ({ret:+.1f}%) "
                 f"hold {hold_h:.0f}h  cum ${self.cum_pnl:+.2f}")
        if lot in self.lots:
            self.lots.remove(lot)
        self._save()

    def _maybe_summary(self):
        """
        Periodic digest, with two cadences.

        While positions are open the 6-hourly P&L digest is what matters -- the book is
        still moving. Once the book is FLAT and halted there is no P&L to report and the
        only live question is whether volatility has come back into range, so it drops to
        a once-daily volatility watch. The barrier crossing itself is NOT on this clock:
        that alert fires immediately from _update_collective_vol().
        """
        flat = not self.lots and self.halted()
        every_h = 24 if flat else SUMMARY_EVERY_H
        slot = f"{every_h}-{int(time.time() // (every_h * 3600))}"
        if self._last_summary_slot == slot:
            return
        self._last_summary_slot = slot

        if flat:
            cv = self.collective_vol
            hist = [h for h in (self.cv_history or [])][-24:]
            rng = ""
            if len(hist) > 1:
                rng = (f"\nlast {len(hist)}h: {100*min(hist):.2f}% - "
                       f"{100*max(hist):.2f}%")
            trend = ""
            if len(hist) >= 12:
                first, last = _median(hist[:6]), _median(hist[-6:])
                if first and last:
                    trend = ("\ntrend: falling" if last < first * 0.97
                             else "\ntrend: rising" if last > first * 1.03
                             else "\ntrend: flat")
            body = (f"collective vol <b>{100*cv:.2f}%/h</b>" if cv
                    else "collective vol: not yet measured")
            # Daily digest silenced at the user's request (2026-10-26). The crossing
            # itself still alerts immediately from _update_collective_vol().
            self.log(f"daily vol watch (not sent): {body}{rng}{trend}")
        else:
            try:
                import provision_status
                body = provision_status.live(self.datadir)
            except Exception as e:           # noqa: BLE001 - never break trading
                body = f"(status unavailable: {e})"
            self.notify(f"\U0001F4C8 <b>{SUMMARY_EVERY_H}h summary</b>\n{body}")
        self._save()

    def _update_collective_vol(self, sigmas):
        """
        Cross-sectional median trailing sigma across the whole Day 3-45 cohort, and the
        all-clear alert when it returns to normal.

        Measured over the FULL universe rather than the coins traded: coins only trade
        because they dipped 5% in an hour, so measuring on those would sample the
        volatile subset and read systematically high -- an error that inflated an
        earlier reading from the 96th to the 99th percentile.
        """
        if len(sigmas) < 5:
            return
        cv = _median(sigmas)
        prev = self.collective_vol
        self.collective_vol = cv
        self.cv_history = (self.cv_history + [cv])[-48:]
        arrow = ""
        if prev:
            arrow = " (falling)" if cv < prev else " (rising)"
        self.log(f"collective vol {100*cv:.2f}%/h across {len(sigmas)} coins"
                 f"{arrow}  [normal < {100*CV_RESUME_PCT:.2f}%]")
        if cv < CV_RESUME_PCT and not self._cv_alerted:
            self._cv_alerted = True
            self.notify(
                f"\U0001F7E9 <b>collective vol back to normal</b>\n"
                f"{100*cv:.2f}%/h, below the {100*CV_RESUME_PCT:.2f}% line for the "
                f"first time since this halt.\n\n"
                f"The strategy was validated in this regime. It is still HALTED and "
                f"will NOT restart on its own -- send /resume if you want to "
                f"reallocate.\nCheck /live first: one clean hour is not a regime.")
        elif cv >= CV_RESUME_PCT * 1.15 and self._cv_alerted:
            self._cv_alerted = False        # re-arm once it is clearly back above

    def _check_stop_rules(self):
        """
        Enforce the pre-committed stopping rule. Halts entries only.

        Net is marked to market because realised alone is structurally flattering here:
        winners exit on target within hours while losers sit to the 72h backstop, so the
        closed set leads and the open book lags.
        """
        if self.halted():
            return
        reason = None
        if self.n_closed >= STOP_CLOSED:
            reason = f"reached {self.n_closed} closed lots (limit {STOP_CLOSED})"
        else:
            unreal, priced = 0.0, 0
            if self.lots:
                try:
                    px = self.cli.prices()
                except ExchangeError:
                    return                      # no marks, no decision
                for l in self.lots:
                    p = px.get(l["symbol"])
                    if not p:
                        continue
                    unreal += float(l["qty"]) * float(p) - float(l["cost_usd"])
                    priced += 1
                if priced < len(self.lots) * 0.8:
                    return                      # too little of the book priced to judge
            net = self.cum_pnl + unreal
            if net <= STOP_NET_USD:
                reason = (f"net ${net:+.2f} hit the ${STOP_NET_USD:+.0f} limit "
                          f"(realised ${self.cum_pnl:+.2f}, unrealised ${unreal:+.2f})")
        if reason:
            self.log(f"STOP RULE TRIGGERED: {reason}")
            self._halt(f"stop rule: {reason}")
            self.notify(f"\U0001F6A9 <b>PRE-COMMITTED STOP TRIGGERED</b>\n{reason}\n\n"
                        f"New entries are HALTED. The {len(self.lots)} open lots will "
                        f"still exit on target or at the {MAX_HOLD_H}h backstop -- "
                        f"nothing is being force-sold.\n"
                        f"/live to watch it wind down, /resume to override.")

    def _halt(self, why):
        with open(self.halt_file, "w") as f:
            f.write(f"{iso(now_ms())} {why}\n")
        self.notify(f"⛔ HALTED: {why}\nNo new entries. Positions still exit.")

    # ---------------- main loop ----------------
    def run(self):
        mode = "LIVE (real orders)" if self.live else "DRY RUN (no orders sent)"
        self.log(f"=== provision executor starting | {mode} | lot ${self.lot} | "
                 f"dip {DIP:.0%} tp {TP:.0%} hold {MAX_HOLD_H}h | "
                 f"cap ${MAX_EXPOSURE_USD:.0f} ===")
        self.cli.sync_time()
        self.cli.exchange_info()
        self.reconcile(first=True)
        self._last_recon = time.time()
        self.notify(f"\U0001F916 executor started -- {mode}\n"
                    f"lot ${self.lot}, {len(self.lots)} open, cum ${self.cum_pnl:+.2f}")
        self.refresh_levels()
        while True:
            try:
                bar_now = int(time.time()) // BAR_SECS * BAR_SECS
                if self._last_bar != bar_now and \
                        time.time() - bar_now >= CYCLE_OFFSET:
                    self.refresh_levels()
                if os.path.exists(self.flatten_file):
                    try:
                        why = open(self.flatten_file).read().strip()[:80]
                    except Exception:            # noqa: BLE001
                        why = "telegram"
                    os.remove(self.flatten_file)  # consume first: never loop on it
                    self.flatten_all(why or "telegram")
                self.scan()
                self.manage()
                if time.time() - self._last_recon > RECONCILE_EVERY_S:
                    self.reconcile()
                    self._check_stop_rules()
                    self._last_recon = time.time()
                self._maybe_summary()
            except Exception as e:           # noqa: BLE001
                self.log(f"ERROR loop: {e}")
                self.notify(f"⚠️ loop error: {e}")
            time.sleep(SCAN_SECS)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--datadir", default="./live_provision")
    ap.add_argument("--lot", type=float, default=2.0, help="USD per lot")
    ap.add_argument("--label", default=None)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True,
                   help="log intended orders, send nothing (DEFAULT)")
    g.add_argument("--live", action="store_true",
                   help="SEND REAL ORDERS. Requires MEXC_API_KEY / MEXC_API_SECRET.")
    a = ap.parse_args()
    Executor(a.datadir, a.lot, live=bool(a.live), label=a.label).run()
