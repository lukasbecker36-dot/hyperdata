#!/usr/bin/env python3
"""Replay a same-direction concurrency cap on the ACTUAL live paper trades.

Model: process trades in ENTRY order. Each trade occupies [entry, entry+hold].
When a new signal arrives, count how many positions of the SAME side are still
open at that instant. If that count >= cap, SKIP the trade (its real P&L is
removed). Skipping does not consume a slot. We measure the counterfactual total.

This uses real fills / real exits / real P&L, so the only assumption is
"a hit cap means we wouldn't have entered." No exit-rule modelling.
"""
import csv, sys
from datetime import datetime

def load(fn):
    rows=[]
    with open(fn) as f:
        for r in csv.DictReader(f):
            rows.append({
                "sym": r["symbol"], "side": r["side"],
                "entry": datetime.strptime(r["entry_time"], "%Y-%m-%d %H:%M:%S"),
                "hold_h": float(r["hold_h"]), "pnl": float(r["pnl_usd"]),
                "reason": r["reason"], "net_bps": float(r["net_bps"]),
                "cum": float(r["cum_pnl"]),
            })
    return rows

def exit_time(t):
    return t["entry"].timestamp() + t["hold_h"]*3600

def simulate(trades, cap, side_scope="SHORT"):
    """cap = max concurrent same-`side_scope` positions. side_scope in {SHORT,LONG,BOTH}.
    Returns (kept, skipped, total_pnl, skipped_pnl)."""
    order = sorted(trades, key=lambda t: t["entry"])
    open_pos = []   # list of (exit_ts, side) for KEPT trades
    kept=[]; skipped=[]
    for t in order:
        now = t["entry"].timestamp()
        open_pos = [p for p in open_pos if p[0] > now]   # expire
        if side_scope == "BOTH":
            same = len(open_pos)
        else:
            same = sum(1 for p in open_pos if p[1] == side_scope)
        gated = (t["side"] == side_scope) or (side_scope == "BOTH")
        if gated and same >= cap:
            skipped.append(t)
        else:
            kept.append(t)
            open_pos.append((exit_time(t), t["side"]))
    tot = sum(t["pnl"] for t in kept)
    skpnl = sum(t["pnl"] for t in skipped)
    return kept, skipped, tot, skpnl

def report(name, fn):
    trades = load(fn)
    base = sum(t["pnl"] for t in trades)
    reclaim = sum(t["pnl"] for t in trades if t["reason"]=="reclaim")
    backstop = sum(t["pnl"] for t in trades if t["reason"]=="backstop")
    nb = sum(1 for t in trades if t["reason"]=="backstop")
    # integrity check vs state file cum
    last_cum = trades[-1]["cum"]
    print(f"\n{'='*70}\n{name}  ({len(trades)} trades)")
    print(f"  transcription check: sum(pnl)={base:+.2f}  vs last cum col={last_cum:+.2f}  "
          f"-> {'OK' if abs(base-last_cum)<0.02 else 'MISMATCH!'}")
    print(f"  baseline (no cap):   total={base:+.2f}   reclaim={reclaim:+.2f}  "
          f"backstop={backstop:+.2f} ({nb} trades)")
    print(f"\n  {'cap':>4} {'scope':>6} | {'total':>8} {'vs base':>8} | "
          f"{'kept':>4} {'skip':>4} {'skipPnL':>8} | {'skip-recl':>9} {'skip-back':>9}")
    for scope in ("SHORT","BOTH"):
        for cap in (1,2,3,4,5,8):
            kept,skipped,tot,skpnl = simulate(trades, cap, scope)
            sk_r = sum(t["pnl"] for t in skipped if t["reason"]=="reclaim")
            sk_b = sum(t["pnl"] for t in skipped if t["reason"]=="backstop")
            print(f"  {cap:>4} {scope:>6} | {tot:+8.2f} {tot-base:+8.2f} | "
                  f"{len(kept):>4} {len(skipped):>4} {skpnl:+8.2f} | {sk_r:+9.2f} {sk_b:+9.2f}")
    # show which backstop losers a SHORT cap=2 would have removed
    kept,skipped,tot,_ = simulate(trades, 2, "SHORT")
    sk_back = sorted([t for t in skipped if t["reason"]=="backstop"], key=lambda x:x["pnl"])
    print(f"\n  SHORT cap=2 -> skipped backstop losers:")
    for t in sk_back:
        print(f"     {t['entry']:%m-%d %H:%M} {t['sym']:9s} {t['side']:5s} {t['pnl']:+8.2f}")

report("5m book", sys.argv[1])
report("15m book", sys.argv[2])
