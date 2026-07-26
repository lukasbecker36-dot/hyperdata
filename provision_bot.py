#!/usr/bin/env python3
"""
MEXC new-listing DIP-PROVISION -- autonomous PAPER-TRADING bot.

Strategy (Phase 9 of the perpshort study; the one configuration that survived every
stress test applied):

  UNIVERSE   crypto USDT perps whose listing age is Day 3-45. Tokenised equities,
             indices and commodities are filtered out, matching the research universe.
  ENTRY      a resting BUY 5% below the prior hourly close. Long only.
  EXIT       a resting SELL at +10% from the fill (maker), or a market exit after
             72 hours (taker). No stop loss.
  SIZE       $100 per lot, at most 5 lots per symbol. Cash is therefore bounded at
             $500 per symbol, so there is NO leverage and NO liquidation path -- which
             is the whole point: the squeeze tail that destroyed every short
             configuration in Phases 1-8 cannot reach a cash-bounded long.
  GATES      the bar must trade at least 20x the lot size (no one-tick wick fills),
             and price must trade 0.2% THROUGH the level, not merely touch it.

Backtest, 219 tokens over 18 months: +$31/token per 42-day window at $100 lots, a
positive median, ~46% return on average deployed capital, positive in all six launch
quarters, surviving a token-clustered bootstrap, a 100x volume gate, a 50% fill
haircut and a 1% exit-fill haircut. It beat random entry at identical exposure by
+$35/token (95% CI [+18, +53]), while the same rule at a 2% distance was pure long
beta. See mexc_status.md sections 27-35.

WHY THIS RUNS LIVE
The backtest's one unfixable weakness is that hourly OHLC cannot say whether a
resting bid 5% below the market would ACTUALLY have filled. A bar low proves a trade
printed at that price; it does not prove our order reached the front of the queue.
So this bot measures the thing the backtest had to assume:

  * every order it places snapshots the ORDER BOOK first, recording how much USD is
    resting at or above our level -- the queue we sit behind;
  * while the order is live it polls the trade feed and accumulates SELL-aggressor
    volume printing at or below our level -- the queue-consuming flow;
  * at the bar close it records BOTH verdicts: `bar_fill` (the backtest's assumption,
    low pierced the level) and `queue_fill` (traded-through volume actually exceeded
    the queue ahead of us plus our own size).

P&L is booked on `bar_fill` so the live run is directly comparable to the backtest,
while `orders_*.csv` accumulates the evidence for what the real fill rate is. If
queue_fill runs far below bar_fill, the backtest is optimistic and the report says so.

MEXC charges maker 4bp / taker 10bp on brand-new listings and switches them to zero
maker / 2bp taker later, alongside `apiAllowed`. Both are recorded per trade at
execution time, because the backtest could only read today's schedule, not the one in
force during Day 3-45.

Stdlib only. Paper only: it places no orders and needs no API key.

  python3 provision_bot.py --datadir ./paper_provision
  python3 provision_bot.py --once            # single cycle, for smoke-testing
"""
import argparse
import csv
import json
import math
import os
import time
from datetime import datetime, timezone

import mexc_api as mx

try:
    import telegram_notify as tg          # optional; must never break trading
except Exception:                          # noqa: BLE001
    tg = None

# ---- strategy constants (match provide4.py / provide5.py) ----
DIP           = 0.05      # resting bid this far below the prior hourly close
TP            = 0.10      # resting sell this far above the fill price
MAX_HOLD_H    = 72        # market exit after this long
MAX_LOTS      = 5         # per symbol; caps cash at MAX_LOTS * LOT_USD
LOT_USD       = 100.0
FROM_DAY      = 3         # listing-age window
TO_DAY        = 45
ENTRY_BUFFER  = 0.002     # price must trade THROUGH the level by this much
VOL_GATE      = 20        # bar USD volume must be >= VOL_GATE * LOT_USD
MAX_SYMBOLS   = 40        # safety cap on concurrent symbols

POLL_S        = 20        # trade-feed poll cadence while orders rest
BAR_SECS      = mx.BAR_SECS
CYCLE_OFFSET  = 25        # seconds after the bar close before the hourly cycle
SEEN_KEEP     = 400       # trade ids retained per order for de-duplication
SUMMARY_EVERY_H = 6       # Telegram summary cadence, on UTC boundaries
DUE_SOON_H    = 12        # flag lots this close to the time stop


def now_ms():
    return int(time.time() * 1000)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Bot:
    def __init__(self, datadir, label=None, api_only=False, lot_usd=LOT_USD,
                 verbose=False):
        self.datadir = datadir
        os.makedirs(datadir, exist_ok=True)
        self.api_only = api_only
        self.lot_usd = lot_usd
        self.verbose = verbose      # push a Telegram message per fill/close
        base = os.path.basename(os.path.normpath(datadir))
        self.label = label or (base[6:] if base.startswith("paper_") else base) or "provision"
        self.trade_csv = os.path.join(datadir, "trades.csv")
        self.order_csv = os.path.join(datadir, "orders.csv")
        self.state_file = os.path.join(datadir, "state.json")
        self.log_file = os.path.join(datadir, "bot.log")

        self.universe = {}          # contract -> meta
        self.orders = {}            # contract -> pending resting order
        self.lots = []              # open long lots
        self.cum_pnl = 0.0
        self.n_closed = 0
        self.n_win = 0
        self.n_orders = 0
        self.n_barfill = 0
        self.n_queuefill = 0
        self.next_lot_id = 1
        self._last_summary_slot = None
        self._last_cycle_bar = None
        # counters since the last Telegram summary
        self.p_fills = 0
        self.p_closes = 0
        self.p_wins = 0
        self.p_pnl = 0.0
        # mark-to-market of the open book, refreshed each cycle
        self.unrealised = 0.0
        self.open_notional = 0.0
        self.lots_due = 0

        self._load_state()
        self._init_csvs()

    # ---------------- logging / state ----------------
    def log(self, msg):
        line = f"{iso(now_ms())}  {msg}"
        print(line, flush=True)
        try:
            with open(self.log_file, "a") as f:
                f.write(line + "\n")
        except Exception:            # noqa: BLE001
            pass

    def notify(self, msg):
        """Best-effort Telegram push, tagged with this arm's label. Never raises."""
        if tg is None or not tg.enabled():
            return
        try:
            tg.send(f"<b>[{self.label}]</b> {msg}")
        except Exception:            # noqa: BLE001
            pass

    def _init_csvs(self):
        if not os.path.exists(self.trade_csv):
            with open(self.trade_csv, "w", newline="") as f:
                csv.writer(f).writerow([
                    "close_time", "base", "contract", "entry_time", "age_days_at_entry",
                    "entry_px", "exit_px", "target_px", "hold_h", "notional",
                    "gross_bps", "fee_bps", "funding_bps", "net_bps", "pnl_usd",
                    "reason", "queue_fill", "queue_ahead_usd", "traded_below_usd",
                    "api_allowed", "maker_fee", "taker_fee", "cum_pnl"])
        if not os.path.exists(self.order_csv):
            with open(self.order_csv, "w", newline="") as f:
                csv.writer(f).writerow([
                    "resolve_time", "base", "contract", "bar_ts", "age_days",
                    "prior_close", "level", "bar_low", "bar_vol_usd",
                    "queue_ahead_usd", "traded_below_usd", "n_prints", "polls",
                    "bar_fill", "queue_fill", "vol_gate_ok", "opened", "skip_reason",
                    "api_allowed"])

    def _save_state(self):
        tmp = self.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"orders": self.orders, "lots": self.lots,
                       "cum_pnl": self.cum_pnl, "n_closed": self.n_closed,
                       "n_win": self.n_win, "n_orders": self.n_orders,
                       "n_barfill": self.n_barfill, "n_queuefill": self.n_queuefill,
                       "next_lot_id": self.next_lot_id,
                       "last_cycle_bar": self._last_cycle_bar,
                       "last_summary_slot": self._last_summary_slot,
                       "p_fills": self.p_fills, "p_closes": self.p_closes,
                       "p_wins": self.p_wins, "p_pnl": self.p_pnl}, f)
        os.replace(tmp, self.state_file)

    def _load_state(self):
        if not os.path.exists(self.state_file):
            return
        try:
            s = json.load(open(self.state_file))
            self.orders = s.get("orders", {})
            self.lots = s.get("lots", [])
            self.cum_pnl = s.get("cum_pnl", 0.0)
            self.n_closed = s.get("n_closed", 0)
            self.n_win = s.get("n_win", 0)
            self.n_orders = s.get("n_orders", 0)
            self.n_barfill = s.get("n_barfill", 0)
            self.n_queuefill = s.get("n_queuefill", 0)
            self.next_lot_id = s.get("next_lot_id", 1)
            self._last_cycle_bar = s.get("last_cycle_bar")
            self._last_summary_slot = s.get("last_summary_slot")
            self.p_fills = s.get("p_fills", 0)
            self.p_closes = s.get("p_closes", 0)
            self.p_wins = s.get("p_wins", 0)
            self.p_pnl = s.get("p_pnl", 0.0)
        except Exception:            # noqa: BLE001
            pass

    def _winrate(self):
        return self.n_win / self.n_closed * 100 if self.n_closed else 0.0

    def _maybe_summary(self):
        """
        Periodic Telegram digest instead of a message per trade.

        Deliberately leads with realised AND unrealised together. Winners exit on the
        target within hours while losers sit to the 72h stop, so realised P&L alone
        reads far too well early in a run -- the first 36 fills here closed 9 winners
        and nothing else. "if closed now" is the honest running figure.
        """
        slot = int(time.time() // (SUMMARY_EVERY_H * 3600))
        if self._last_summary_slot == slot:
            return
        self._last_summary_slot = slot

        fr = (self.n_barfill / self.n_orders * 100) if self.n_orders else 0.0
        qr = (self.n_queuefill / self.n_orders * 100) if self.n_orders else 0.0
        qshare = (self.n_queuefill / self.n_barfill * 100) if self.n_barfill else 0.0
        net_now = self.cum_pnl + self.unrealised
        pwin = (self.p_wins / self.p_closes * 100) if self.p_closes else 0.0
        self.notify(
            f"\U0001F4C8 <b>{SUMMARY_EVERY_H}h summary</b>\n"
            f"<b>if closed now: ${net_now:+.2f}</b>\n"
            f"  realised ${self.cum_pnl:+.2f} ({self.n_closed} closed, "
            f"{self._winrate():.0f}% win)\n"
            f"  unrealised ${self.unrealised:+.2f} ({len(self.lots)} open, "
            f"${self.open_notional:,.0f} at risk)\n"
            f"last {SUMMARY_EVERY_H}h: {self.p_fills} buys, {self.p_closes} sells "
            f"({pwin:.0f}% win), ${self.p_pnl:+.2f}\n"
            f"{self.lots_due} lot(s) near the {MAX_HOLD_H}h stop\n"
            f"fills: {self.n_barfill}/{self.n_orders} orders ({fr:.1f}%)\n"
            f"queue-confirmed: {self.n_queuefill} ({qr:.1f}%) "
            f"= {qshare:.0f}% of fills")
        self.p_fills = self.p_closes = self.p_wins = 0
        self.p_pnl = 0.0

    # ---------------- universe ----------------
    def refresh_universe(self):
        detail = mx.contract_detail()
        uni = mx.new_listings(FROM_DAY, TO_DAY, detail=detail)
        if self.api_only:
            uni = {k: v for k, v in uni.items() if v["api_allowed"]}
        # keep symbols we still hold, even if they aged out of the entry window
        held = {l["contract"] for l in self.lots}
        for c in held:
            if c not in uni:
                for d in detail:
                    if d["symbol"] == c:
                        m = mx.new_listings(0, 10_000, detail=[d]).get(c)
                        if m:
                            m["aged_out"] = True
                            uni[c] = m
                        break
        if len(uni) > MAX_SYMBOLS:
            # oldest first: they have the shortest remaining window, so prefer them
            keep = sorted(uni.items(), key=lambda kv: -kv[1]["age_days"])[:MAX_SYMBOLS]
            uni = dict(keep)
        self.universe = uni
        n_api = sum(1 for v in uni.values() if v["api_allowed"])
        self.log(f"universe: {len(uni)} symbols in Day {FROM_DAY}-{TO_DAY} "
                 f"({n_api} apiAllowed, {len(uni)-n_api} not)")

    # ---------------- order polling (the fill measurement) ----------------
    def poll_orders(self):
        """
        Accumulate SELL-aggressor volume printing at or below each resting level.

        A resting BUY fills only when a seller crosses down into it, so buy-aggressor
        prints are irrelevant no matter what price they occur at. De-duplicated by
        trade id because /deals returns a rolling window that overlaps between polls.
        """
        for contract, o in list(self.orders.items()):
            meta = self.universe.get(contract)
            if meta is None:
                continue
            try:
                rows = mx.deals(contract, limit=100)
            except Exception as e:      # noqa: BLE001
                self.log(f"WARN deals {contract}: {e}")
                continue
            seen = set(o.get("seen", []))
            new_ids = []
            for d in rows:
                if d["i"] in seen:
                    continue
                new_ids.append(d["i"])
                if d["t"] < o["placed_ms"]:
                    continue                     # printed before the order existed
                if d["T"] != 2:
                    continue                     # not a sell aggressor
                if d["p"] > o["level"]:
                    continue                     # did not reach our price
                o["traded_below_usd"] += d["p"] * d["v"] * meta["contract_size"]
                o["n_prints"] += 1
            merged = list(seen) + new_ids
            o["seen"] = merged[-SEEN_KEEP:]
            o["polls"] = o.get("polls", 0) + 1
            time.sleep(mx.REQUEST_SLEEP)

    # ---------------- helpers ----------------
    @staticmethod
    def _bar(rows, ts):
        for r in rows:
            if r[0] == ts:
                return r
        return None

    def _contracts_for_lot(self, meta, level):
        """
        MEXC trades in whole contracts, so the lot has to be an integer multiple of
        contract_size coins. Returns (n_contracts, notional) or (0, 0) if one contract
        already costs more than twice the lot budget -- in which case the symbol is
        untradeable at this size and is skipped rather than silently upsized.
        """
        one = level * meta["contract_size"]
        if one <= 0:
            return 0, 0.0
        n = int(math.floor(self.lot_usd / one))
        if n < 1:
            if one <= 2 * self.lot_usd:
                n = 1
            else:
                return 0, 0.0
        return n, n * one

    def _funding_over(self, contract, t0, t1):
        """Summed funding rate settled in (t0, t1]. Positive = longs paid."""
        try:
            hist = mx.funding_history(contract, page_size=100, max_pages=2)
        except Exception as e:          # noqa: BLE001
            self.log(f"WARN funding {contract}: {e}")
            return 0.0
        return sum(r for ts, r in hist if t0 < ts <= t1)

    # ---------------- trade ops ----------------
    def open_lot(self, meta, level, order, bar_ts):
        n, notional = self._contracts_for_lot(meta, level)
        if n < 1:
            return None, "min_contract_size"
        lot = {
            "id": self.next_lot_id, "contract": meta["contract"], "base": meta["base"],
            "entry_px": level, "n_contracts": n, "notional": notional,
            "units": n * meta["contract_size"],
            "entry_ms": (bar_ts + BAR_SECS) * 1000, "entry_bar_ts": bar_ts,
            "checked_ts": bar_ts, "target": level * (1 + TP),
            "maker_fee": meta["maker_fee"], "taker_fee": meta["taker_fee"],
            "api_allowed": meta["api_allowed"],
            "age_days_at_entry": round(meta["age_days"], 2),
            "queue_fill": bool(order.get("queue_fill")),
            "queue_ahead_usd": round(order.get("queue_ahead_usd", 0.0), 2),
            "traded_below_usd": round(order.get("traded_below_usd", 0.0), 2),
        }
        self.next_lot_id += 1
        self.lots.append(lot)
        self.p_fills += 1
        self.log(f"FILL  {meta['base']:14s} {n:>4}c @ {level:.8g}  "
                 f"${notional:.0f} tgt {lot['target']:.8g}  "
                 f"queue_fill={'Y' if lot['queue_fill'] else 'N'}  "
                 f"lots={len(self.lots)}")
        if self.verbose:
            self.notify(f"\U0001F7E2 FILL <b>{meta['base']}</b> ${notional:.0f} "
                        f"@ {level:.6g}\ntarget {lot['target']:.6g}  "
                        f"age {meta['age_days']:.0f}d  "
                        f"queue-fill {'YES' if lot['queue_fill'] else 'NO'}\n"
                        f"open lots: {len(self.lots)}")
        return lot, None

    def close_lot(self, lot, exit_px, reason, exit_ms):
        maker, taker = lot["maker_fee"], lot["taker_fee"]
        fee_rate = maker if reason == "target" else taker
        gross = (exit_px - lot["entry_px"]) / lot["entry_px"]
        # entry was a resting bid (maker), exit is maker on target / taker on stop
        fee = maker + fee_rate
        fund = self._funding_over(lot["contract"], lot["entry_ms"] // 1000,
                                  exit_ms // 1000)
        net = gross - fee - fund          # a long pays funding when the rate is positive
        pnl = lot["notional"] * net
        self.cum_pnl += pnl
        self.n_closed += 1
        self.n_win += 1 if net > 0 else 0
        self.p_closes += 1
        self.p_wins += 1 if net > 0 else 0
        self.p_pnl += pnl
        hold_h = (exit_ms - lot["entry_ms"]) / 3600000.0
        with open(self.trade_csv, "a", newline="") as f:
            csv.writer(f).writerow([
                iso(exit_ms), lot["base"], lot["contract"], iso(lot["entry_ms"]),
                lot["age_days_at_entry"], f"{lot['entry_px']:.10g}",
                f"{exit_px:.10g}", f"{lot['target']:.10g}", f"{hold_h:.2f}",
                f"{lot['notional']:.2f}", f"{gross*1e4:.1f}", f"{fee*1e4:.1f}",
                f"{fund*1e4:.1f}", f"{net*1e4:.1f}", f"{pnl:.4f}", reason,
                int(lot["queue_fill"]), lot["queue_ahead_usd"],
                lot["traded_below_usd"], int(lot["api_allowed"]),
                maker, taker, f"{self.cum_pnl:.4f}"])
        self.log(f"CLOSE {lot['base']:14s} {reason:8s} net={net*1e4:+7.1f}bps "
                 f"pnl=${pnl:+.3f} hold={hold_h:.0f}h  cum=${self.cum_pnl:+.2f} "
                 f"n={self.n_closed} win={self._winrate():.0f}%")
        if self.verbose:
            emoji = "\U0001F535" if net > 0 else "\U0001F534"
            self.notify(f"{emoji} CLOSE <b>{lot['base']}</b> ({reason})\n"
                        f"net={net*1e4:+.1f}bps  pnl=${pnl:+.3f}  hold={hold_h:.0f}h\n"
                        f"cum=${self.cum_pnl:+.2f}  trades={self.n_closed}  "
                        f"win={self._winrate():.0f}%")

    # ---------------- the hourly cycle ----------------
    def cycle(self):
        bar_now = int(time.time()) // BAR_SECS * BAR_SECS
        closed_ts = bar_now - BAR_SECS
        self.refresh_universe()

        # candles once per symbol, reused for resolution / exits / next level
        candles = {}
        need = {c for c in self.universe} | {l["contract"] for l in self.lots}
        for contract in need:
            meta = self.universe.get(contract)
            if meta is None:
                continue
            try:
                start = max(meta["launch_ts"], bar_now - 200 * BAR_SECS)
                candles[contract] = mx.klines(contract, start, bar_now + BAR_SECS)
            except Exception as e:      # noqa: BLE001
                self.log(f"WARN klines {contract}: {e}")
            time.sleep(mx.REQUEST_SLEEP)

        self._resolve_orders(closed_ts, candles)
        self._manage_lots(closed_ts, candles)
        self._mark_open_lots(closed_ts, candles)
        self._place_orders(bar_now, closed_ts, candles)

        self._last_cycle_bar = bar_now
        self._save_state()
        fr = (self.n_barfill / self.n_orders * 100) if self.n_orders else 0.0
        qr = (self.n_queuefill / self.n_orders * 100) if self.n_orders else 0.0
        self.log(f"cycle done: {len(self.orders)} resting, {len(self.lots)} lots, "
                 f"cum=${self.cum_pnl:+.2f}, closed={self.n_closed}, "
                 f"orders={self.n_orders} (bar-fill {fr:.1f}%, queue-fill {qr:.1f}%)")

    def _resolve_orders(self, closed_ts, candles):
        """Decide, for each order that rested through the closed bar, whether it filled."""
        for contract, o in list(self.orders.items()):
            if o["bar_ts"] != closed_ts:
                if o["bar_ts"] < closed_ts:      # stale (downtime) -> drop unresolved
                    self.log(f"drop stale order {contract} bar={o['bar_ts']}")
                    del self.orders[contract]
                continue
            meta = self.universe.get(contract)
            rows = candles.get(contract) or []
            bar = self._bar(rows, closed_ts)
            del self.orders[contract]
            if meta is None or bar is None:
                continue
            _, _, _, low, close, vol = bar
            bar_vol_usd = vol * meta["contract_size"] * close
            level = o["level"]
            bar_fill = low <= level * (1 - ENTRY_BUFFER)
            vol_ok = bar_vol_usd >= VOL_GATE * self.lot_usd
            queue_fill = (o["traded_below_usd"] >= o["queue_ahead_usd"] + self.lot_usd)
            o["queue_fill"] = queue_fill

            self.n_orders += 1
            self.n_barfill += 1 if (bar_fill and vol_ok) else 0
            self.n_queuefill += 1 if queue_fill else 0

            opened, skip = 0, ""
            if bar_fill and vol_ok:
                n_lots = sum(1 for l in self.lots if l["contract"] == contract)
                if n_lots >= MAX_LOTS:
                    skip = "max_lots"
                else:
                    lot, err = self.open_lot(meta, level, o, closed_ts)
                    opened, skip = (1, "") if lot else (0, err or "")
            elif not bar_fill:
                skip = "not_reached"
            else:
                skip = "vol_gate"

            with open(self.order_csv, "a", newline="") as f:
                csv.writer(f).writerow([
                    iso(now_ms()), meta["base"], contract, closed_ts,
                    round(meta["age_days"], 2), f"{o['prior_close']:.10g}",
                    f"{level:.10g}", f"{low:.10g}", f"{bar_vol_usd:.2f}",
                    f"{o['queue_ahead_usd']:.2f}", f"{o['traded_below_usd']:.2f}",
                    o.get("n_prints", 0), o.get("polls", 0),
                    int(bar_fill and vol_ok), int(queue_fill), int(vol_ok),
                    opened, skip, int(meta["api_allowed"])])

    def _manage_lots(self, closed_ts, candles):
        """Resting-sell target on any bar after entry, else the 72h market exit."""
        for lot in list(self.lots):
            rows = candles.get(lot["contract"]) or []
            hit = None
            for r in rows:
                ts, _, high, _, close, _ = r
                if ts <= lot["checked_ts"] or ts > closed_ts:
                    continue
                if high >= lot["target"]:
                    hit = ts
                    break
            if hit is not None:
                self.close_lot(lot, lot["target"], "target", (hit + BAR_SECS) * 1000)
                self.lots.remove(lot)
                continue
            lot["checked_ts"] = max(lot["checked_ts"], closed_ts)
            if now_ms() - lot["entry_ms"] >= MAX_HOLD_H * 3600 * 1000:
                bar = self._bar(rows, closed_ts) or (rows[-1] if rows else None)
                if bar is None:
                    continue
                self.close_lot(lot, bar[4], "time_stop", (closed_ts + BAR_SECS) * 1000)
                self.lots.remove(lot)

    def _mark_open_lots(self, closed_ts, candles):
        """
        Mark the open book to the latest close.

        This is the half of the run that cannot report itself: a winner closes as soon
        as it prints +10%, often within hours, while a loser sits untouched until the
        72h stop. Realised P&L therefore runs ahead of reality early on, and the
        summary needs the unrealised side next to it to be honest.
        """
        unreal, notional, due = 0.0, 0.0, 0
        for lot in self.lots:
            rows = candles.get(lot["contract"]) or []
            bar = self._bar(rows, closed_ts) or (rows[-1] if rows else None)
            if bar is None:
                continue
            unreal += lot["units"] * (bar[4] - lot["entry_px"])
            notional += lot["notional"]
            if now_ms() - lot["entry_ms"] >= (MAX_HOLD_H - DUE_SOON_H) * 3600 * 1000:
                due += 1
        self.unrealised, self.open_notional, self.lots_due = unreal, notional, due

    def _place_orders(self, bar_now, closed_ts, candles):
        """
        Place a resting bid for the bar now beginning, at DIP below the bar that just
        closed. The order book is snapshotted first so we know the queue we sit behind
        -- that snapshot is the whole reason this runs live.
        """
        for contract, meta in self.universe.items():
            if meta.get("aged_out"):
                continue
            if contract in self.orders:
                continue
            if sum(1 for l in self.lots if l["contract"] == contract) >= MAX_LOTS:
                continue
            rows = candles.get(contract) or []
            bar = self._bar(rows, closed_ts)
            if bar is None:
                continue
            prior_close = bar[4]
            if prior_close <= 0:
                continue
            level = prior_close * (1 - DIP)
            n, _ = self._contracts_for_lot(meta, level)
            if n < 1:
                continue
            try:
                bids, _ = mx.depth(contract, limit=200)
            except Exception as e:      # noqa: BLE001
                self.log(f"WARN depth {contract}: {e}")
                continue
            self.orders[contract] = {
                "level": level, "prior_close": prior_close,
                "bar_ts": bar_now, "placed_ms": now_ms(),
                "queue_ahead_usd": mx.queue_ahead_usd(bids, level, meta["contract_size"]),
                "traded_below_usd": 0.0, "n_prints": 0, "polls": 0, "seen": [],
            }
            time.sleep(mx.REQUEST_SLEEP)
        self.log(f"placed {len(self.orders)} resting bids for bar {iso(bar_now*1000)}")

    # ---------------- main loop ----------------
    def run(self, once=False):
        self.log(f"=== provision paper bot [{self.label}] starting | "
                 f"dip={DIP:.0%} tp={TP:.0%} hold={MAX_HOLD_H}h lot=${self.lot_usd:.0f} "
                 f"maxlots={MAX_LOTS} day{FROM_DAY}-{TO_DAY} "
                 f"buffer={ENTRY_BUFFER:.1%} volgate={VOL_GATE}x "
                 f"api_only={self.api_only} ===")
        self.notify(f"\U0001F916 provision bot started\n"
                    f"dip {DIP:.0%} / tp {TP:.0%} / {MAX_HOLD_H}h, "
                    f"${self.lot_usd:.0f} x {MAX_LOTS} lots\n"
                    f"resuming: cum=${self.cum_pnl:+.2f}  lots={len(self.lots)}  "
                    f"closed={self.n_closed}")
        if once:
            self.cycle()
            return
        # Load the universe BEFORE the poll loop. poll_orders() needs contract
        # metadata (contract_size) to value prints, and orders restored from state
        # after a restart would otherwise be silently skipped -- collecting no tape
        # until the next hourly cycle, which is precisely the measurement this run
        # exists to make.
        try:
            self.refresh_universe()
        except Exception as e:              # noqa: BLE001
            self.log(f"WARN initial universe load: {e}")
        while True:
            nxt = (int(time.time()) // BAR_SECS + 1) * BAR_SECS + CYCLE_OFFSET
            while time.time() < nxt:
                t0 = time.time()
                if self.orders:
                    try:
                        self.poll_orders()
                        self._save_state()
                    except Exception as e:      # noqa: BLE001
                        self.log(f"ERROR poll: {e}")
                slp = POLL_S - (time.time() - t0)
                time.sleep(max(1.0, min(slp, max(1.0, nxt - time.time()))))
            t0 = time.time()
            try:
                self.cycle()
            except Exception as e:              # noqa: BLE001
                self.log(f"ERROR cycle: {e}")
                self.notify(f"⚠️ cycle error: {e}")
            self._maybe_summary()
            self.log(f"cycle took {time.time()-t0:.1f}s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--datadir", default="./paper_provision")
    ap.add_argument("--label", default=None, help="Telegram tag for this arm")
    ap.add_argument("--lot", type=float, default=LOT_USD, help="USD per lot")
    ap.add_argument("--api-only", action="store_true",
                    help="trade only apiAllowed symbols (the live-tradeable subset)")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--verbose", action="store_true",
                    help=f"push a Telegram message per fill and close. Off by default: "
                         f"~40-50/day, and per-trade P&L misleads early in a run. "
                         f"A digest goes out every {SUMMARY_EVERY_H}h regardless.")
    a = ap.parse_args()
    Bot(a.datadir, label=a.label, api_only=a.api_only, lot_usd=a.lot,
        verbose=a.verbose).run(once=a.once)
