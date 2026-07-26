#!/usr/bin/env python3
"""Compare sizing rules by RE-WEIGHTING one arm's trade log. No second bot needed.

Why not run a second live arm: Hyperliquid nets to one position per coin per account.
Two arms would share a book, queue behind each other at the same price (corrupting the
fill-rate comparison), cross-contaminate fee attribution via userFills, and share one
isolated-margin bucket per coin. Sub-accounts would be needed, which means splitting
capital and a master-wallet transfer.

Why re-weighting is equivalent (and better): at these sizes we are not moving the market,
so the sizing rule does not change which signals fire, the price they fill at, or the bps
each trade returns. It only changes the weight on each trade. So every rule's P&L is
recoverable from a single log:

    P&L(rule) = sum over trades of  notional(rule) * net_bps

What re-weighting CANNOT capture, and is reported separately:
    a bigger resting order needs more volume to print through it, so size genuinely
    affects FILL PROBABILITY. That is a real size effect (the audit saw 68% fill on the
    biggest paper bets vs 79% on the smallest) and it is measured here directly from the
    trade log's entry_wait_s plus the missed-entry log, not assumed away.

  python3 analysis/sizing_ab.py <trades.csv> [missed.csv] [base_notional]
"""
import csv, math, os, sys

TRADES = sys.argv[1] if len(sys.argv) > 1 else "live_15m_ats/trades_15m.csv"
MISSED = sys.argv[2] if len(sys.argv) > 2 else None
BASE   = float(sys.argv[3]) if len(sys.argv) > 3 else 25.0
SIZE_REF, SIZE_MIN, SIZE_MAX = 2.0, 0.5, 3.0


def clamp(x):
    return min(SIZE_MAX, max(SIZE_MIN, x))


rows = []
for r in csv.DictReader(open(TRADES)):
    try:
        net = float(r["net_bps"])
        pnl = float(r["pnl_usd"])
        if abs(net) < 1e-9:
            continue
        ntl = pnl / (net / 1e4)
        mult = ntl / BASE                     # the multiplier the bot actually applied
        rows.append(dict(sym=r["symbol"], net=net, pnl=pnl, ntl=ntl, mult=mult,
                         reason=r.get("reason", ""),
                         wait=float(r["entry_wait_s"]) if r.get("entry_wait_s") else None,
                         taker=r.get("exit_taker", ""), fee=r.get("fee_usd", "")))
    except Exception:
        pass
if not rows:
    print(f"no usable trades in {TRADES}"); sys.exit(0)


def st(xs):
    n = len(xs)
    if n < 2: return (float('nan'), float('nan'), n)
    m = sum(xs)/n
    sd = (sum((x-m)**2 for x in xs)/(n-1))**0.5
    return (m, m/(sd/math.sqrt(n)) if sd > 0 else float('nan'), n)


print(f"{os.path.basename(TRADES)}: {len(rows)} closed trades, base notional ${BASE:.0f}\n")

# ---- the re-weighting ----
# ats multiplier is recovered from the applied notional; the bot derived it as
# clamp(ats_ratio / SIZE_REF), so ats_ratio = mult * SIZE_REF and the inverse rule is
# clamp(SIZE_REF / ats_ratio) = clamp(1 / mult).
rules = {
    "flat (no size rule)": lambda t: 1.0,
    "ats  (live bot)    ": lambda t: t["mult"],
    "INVERSE ats        ": lambda t: clamp(1.0 / t["mult"]) if t["mult"] > 0 else 1.0,
}
print(f"  {'rule':>20} {'avg size':>9} {'total $':>10} {'$/trade':>9} {'bps/unit':>9} {'t':>7}")
for name, f in rules.items():
    pnl = [f(t) * BASE * t["net"] / 1e4 for t in rows]
    sz = [f(t) for t in rows]
    m, t, n = st(pnl)
    print(f"  {name} {sum(sz)/len(sz):>9.2f} {sum(pnl):>+10.2f} {m:>+9.3f} "
          f"{sum(pnl)/(sum(sz)*BASE)*1e4:>+9.1f} {t:>+7.1f}")
print("\n  (bps/unit is size-weighted, so it is the fair comparison -- a 3x bet that")
print("   returns +10bps contributes 3x the dollars AND 3x the cost.)\n")

# ---- what re-weighting cannot capture: size vs fill ----
print("  --- size vs FILL (the part re-weighting cannot recover) ---")
w = [t for t in rows if t["wait"] is not None]
if len(w) >= 6:
    w.sort(key=lambda t: t["mult"])
    k = max(1, len(w)//2)
    for lab, seg in (("smaller half", w[:k]), ("larger half", w[k:])):
        mw, _, n = st([t["wait"] for t in seg])
        print(f"    {lab:>13}: n={n:<3} mult {seg[0]['mult']:.2f}..{seg[-1]['mult']:.2f}"
              f"  mean entry wait {mw:.0f}s")
    print("    (longer wait on bigger bets = the size penalty on fill probability)")
else:
    print(f"    only {len(w)} trades carry entry_wait_s -- need more before this says anything")
if MISSED and os.path.exists(MISSED):
    miss = list(csv.DictReader(open(MISSED)))
    print(f"    missed entries: {len(miss)}  vs filled: {len(rows)}"
          f"  -> fill rate {len(rows)/(len(rows)+len(miss))*100:.0f}%"
          f"  (tape predicted ~76%)")
    if miss:
        mm = [float(m["sz"]) * 0 + 1 for m in miss]      # placeholder count
        print("    NOTE: misses carry no P&L, so a rule that bets bigger on the signals")
        print("    least likely to fill loses trades outright -- check miss composition")
        print("    once there are enough of them.")
else:
    print("    no missed-entry log given; pass it as argv[2] to get the live fill rate")

tk = [t for t in rows if t["taker"] == "1"]
print(f"\n  exits that had to cross the spread: {len(tk)}/{len(rows)}"
      f"  ({len(tk)/len(rows)*100:.0f}%)  -- a cost no backtest charged")
