#!/usr/bin/env python3
"""
Report on the live provision paper run -- and specifically on the one thing the
backtest could not answer.

The Phase 9 backtest assumed a resting bid filled whenever the hourly bar's low
pierced it. That assumption is bracketed here from both sides, using data only a
live run can produce:

  bar_fill    the backtest's rule: the low pierced the level (and the volume gate
              passed). OPTIMISTIC -- a trade printed at our price, but says nothing
              about whether our order was at the front of the queue.
  queue_fill  sell-aggressor volume printing at or below our level exceeded the USD
              resting at or above it when we placed, plus our own size. CONSERVATIVE
              -- it charges us for the whole visible book being *traded* through,
              when in reality much of it is cancelled rather than filled.

The true fill rate lies between them. If queue_fill is far below bar_fill, the
backtest overstates the strategy and the P&L on queue-confirmed fills is the number
to believe.

Backtest expectations to compare against (mexc_status.md sections 31-34, at $100
lots, 5% dip / 10% resting TP / 72h stop, 20x volume gate, 0.2% buffer):
  ~23 fills per token per 42 days, ~56% of fills exit on the target,
  +$31 per token per 42 days, ~56% of tokens profitable.

  python3 provision_report.py [datadir]
"""
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

DATADIR = sys.argv[1] if len(sys.argv) > 1 else "./paper_provision"
BT_FILLS_PER_TOKEN_42D = 23.0
BT_TP_RATE = 56.0
BT_PNL_PER_TOKEN_42D = 31.0


def read(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def fnum(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def pct(a, b):
    return 100.0 * a / b if b else 0.0


def quantiles(vals, qs=(0.25, 0.5, 0.75)):
    if not vals:
        return [float("nan")] * len(qs)
    s = sorted(vals)
    return [s[min(len(s) - 1, int(q * len(s)))] for q in qs]


def main():
    orders = read(os.path.join(DATADIR, "orders.csv"))
    trades = read(os.path.join(DATADIR, "trades.csv"))
    print(f"datadir: {DATADIR}")
    if not orders and not trades:
        print("no data yet -- the bot writes orders.csv every hour and trades.csv on exits.")
        return

    # ---------------- 1. the fill-rate bracket ----------------
    print("\n" + "=" * 104)
    print("1. FILL RATE -- the assumption the backtest could not test")
    print("=" * 104)
    n = len(orders)
    if n:
        bar = sum(1 for o in orders if o["bar_fill"] == "1")
        que = sum(1 for o in orders if o["queue_fill"] == "1")
        both = sum(1 for o in orders if o["bar_fill"] == "1" and o["queue_fill"] == "1")
        gate = sum(1 for o in orders if o["vol_gate_ok"] == "1")
        opened = sum(1 for o in orders if o["opened"] == "1")
        span_h = 0
        ts = sorted({int(fnum(o["bar_ts"])) for o in orders})
        if len(ts) > 1:
            span_h = (ts[-1] - ts[0]) / 3600.0
        print(f"  orders resolved       : {n:,}  over {span_h:.0f} bar-hours, "
              f"{len({o['base'] for o in orders})} symbols")
        print(f"  bar_fill  (optimistic): {bar:>6,}  {pct(bar,n):>5.1f}%")
        print(f"  queue_fill(conservative): {que:>4,}  {pct(que,n):>5.1f}%")
        print(f"  both agree            : {both:>6,}  {pct(both,n):>5.1f}%")
        print(f"  volume gate passed    : {gate:>6,}  {pct(gate,n):>5.1f}%")
        print(f"  lots actually opened  : {opened:>6,}  {pct(opened,n):>5.1f}%")
        if bar:
            print(f"\n  queue-confirmed share of assumed fills: {pct(both,bar):.1f}%")
            print("  -> multiply backtest P&L by roughly this to get the pessimistic case.")
        # fill rate vs how deep the queue was
        rows = [(fnum(o["queue_ahead_usd"]), o["bar_fill"] == "1", o["queue_fill"] == "1")
                for o in orders]
        rows.sort()
        print(f"\n  {'queue ahead ($) quartile':<28}{'n':>7}{'bar_fill':>10}{'queue_fill':>12}")
        for i, name in enumerate(["Q1 thinnest", "Q2", "Q3", "Q4 deepest"]):
            lo, hi = i * len(rows) // 4, (i + 1) * len(rows) // 4
            seg = rows[lo:hi]
            if not seg:
                continue
            print(f"  {name:<12}{seg[0][0]:>8,.0f}-{seg[-1][0]:<7,.0f}{len(seg):>7}"
                  f"{pct(sum(1 for r in seg if r[1]),len(seg)):>9.1f}%"
                  f"{pct(sum(1 for r in seg if r[2]),len(seg)):>11.1f}%")
        reasons = defaultdict(int)
        for o in orders:
            if o["skip_reason"]:
                reasons[o["skip_reason"]] += 1
        if reasons:
            print("\n  non-fills / skips:")
            for r, c in sorted(reasons.items(), key=lambda kv: -kv[1]):
                print(f"    {r:<22}{c:>6,}  {pct(c,n):>5.1f}%")

    # ---------------- 2. closed trades ----------------
    print("\n" + "=" * 104)
    print("2. CLOSED TRADES vs BACKTEST EXPECTATION")
    print("=" * 104)
    if not trades:
        print("  no closed trades yet.")
    else:
        pnls = [fnum(t["pnl_usd"]) for t in trades]
        nets = [fnum(t["net_bps"]) for t in trades]
        holds = [fnum(t["hold_h"]) for t in trades]
        tp = sum(1 for t in trades if t["reason"] == "target")
        wins = sum(1 for p in pnls if p > 0)
        tot = sum(pnls)
        print(f"  closed          : {len(trades):,}")
        print(f"  total P&L       : ${tot:+,.2f}")
        print(f"  mean / median   : ${tot/len(pnls):+.2f} / "
              f"${quantiles(pnls)[1]:+.2f} per trade")
        print(f"  net bps mean    : {sum(nets)/len(nets):+.1f}")
        print(f"  win rate        : {pct(wins,len(pnls)):.1f}%")
        print(f"  exit mix        : target {pct(tp,len(trades)):.1f}%  "
              f"(backtest {BT_TP_RATE:.0f}%),  time_stop "
              f"{pct(len(trades)-tp,len(trades)):.1f}%")
        print(f"  hold hours      : p25 {quantiles(holds)[0]:.0f}  "
              f"med {quantiles(holds)[1]:.0f}  p75 {quantiles(holds)[2]:.0f}")
        fees = sum(fnum(t["fee_bps"]) for t in trades) / len(trades)
        fund = sum(fnum(t["funding_bps"]) for t in trades) / len(trades)
        print(f"  cost per trade  : fees {fees:+.1f}bps, funding {fund:+.1f}bps "
              f"(negative funding = the long was PAID)")

        # the number that matters: P&L on queue-confirmed fills only
        q = [fnum(t["pnl_usd"]) for t in trades if t["queue_fill"] == "1"]
        if q:
            print(f"\n  P&L on queue-confirmed fills only: ${sum(q):+,.2f} over {len(q)} "
                  f"trades (mean ${sum(q)/len(q):+.2f})")
            print(f"  P&L on the rest                  : "
                  f"${tot-sum(q):+,.2f} over {len(trades)-len(q)} trades")
            print("  If the queue-confirmed subset is much worse, the fills the backtest")
            print("  assumed were the profitable ones -- i.e. adverse selection.")

        # per symbol
        by = defaultdict(list)
        for t in trades:
            by[t["base"]].append(fnum(t["pnl_usd"]))
        print(f"\n  {len(by)} symbols traded; per-symbol P&L:")
        print(f"  {'symbol':<14}{'trades':>7}{'P&L':>10}{'mean':>9}")
        for s, v in sorted(by.items(), key=lambda kv: -sum(kv[1]))[:15]:
            print(f"  {s:<14}{len(v):>7}{sum(v):>10,.2f}{sum(v)/len(v):>9,.2f}")
        pos = sum(1 for v in by.values() if sum(v) > 0)
        print(f"  symbols profitable: {pos}/{len(by)} ({pct(pos,len(by)):.0f}%)")

        # fee-tier and api split, since MEXC prices new listings differently
        print(f"\n  {'cohort':<26}{'trades':>8}{'P&L':>11}{'mean bps':>10}{'win%':>7}")
        for label, sel in (
                ("apiAllowed at entry", lambda t: t["api_allowed"] == "1"),
                ("not apiAllowed", lambda t: t["api_allowed"] != "1"),
                ("zero maker fee", lambda t: fnum(t["maker_fee"]) == 0.0),
                ("paid maker fee", lambda t: fnum(t["maker_fee"]) > 0.0)):
            g = [t for t in trades if sel(t)]
            if not g:
                continue
            gp = [fnum(t["pnl_usd"]) for t in g]
            gb = [fnum(t["net_bps"]) for t in g]
            print(f"  {label:<26}{len(g):>8}{sum(gp):>11,.2f}"
                  f"{sum(gb)/len(gb):>10,.1f}{pct(sum(1 for x in gp if x>0),len(gp)):>7.0f}")

    # ---------------- 3. run-rate vs backtest ----------------
    if orders and trades:
        print("\n" + "=" * 104)
        print("3. RUN RATE -- is it trading as often as the backtest said?")
        print("=" * 104)
        nsym = len({o["base"] for o in orders})
        ts = sorted({int(fnum(o["bar_ts"])) for o in orders})
        days = max((ts[-1] - ts[0]) / 86400.0, 1e-9) if len(ts) > 1 else 0.0
        if days > 0.5:
            opened = sum(1 for o in orders if o["opened"] == "1")
            per_tok_42 = opened / nsym / days * 42
            pnl_per_tok_42 = sum(fnum(t["pnl_usd"]) for t in trades) / nsym / days * 42
            print(f"  elapsed {days:.1f} days across {nsym} symbols")
            print(f"  fills per token per 42d : {per_tok_42:.1f}  "
                  f"(backtest {BT_FILLS_PER_TOKEN_42D:.0f})")
            print(f"  P&L  per token per 42d  : ${pnl_per_tok_42:+.2f}  "
                  f"(backtest ${BT_PNL_PER_TOKEN_42D:+.0f})")
            print("\n  Open lots are excluded from P&L, so early readings are biased")
            print("  DOWN: winners exit on the target quickly, losers sit for 72h.")
        else:
            print(f"  only {days*24:.1f} hours of data -- too early for a run rate.")

    print(f"\ngenerated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")


if __name__ == "__main__":
    main()
