#!/usr/bin/env python3
"""Out-of-sample test of the BTC-ER chop filter — leave-one-month-out.

A single recent holdout cannot validate a CHOP filter: the choppy month (May) is in-sample and the last
45 days trended, so there is no chop to avoid. Instead, leave-one-month-out: for each month M, pick the
BTC efficiency-ratio threshold that maximises RAW net on all OTHER months, then apply it to M. Every
month — including the May whipsaw — is thus scored with a threshold chosen WITHOUT seeing it. Pool the
per-month OOS trades for an honest aggregate, reported RAW (beta+alpha) and MARKET-NEUTRAL (alpha only).
Compares against the unfiltered baseline. Imports the tagged trades from ma_pullback_chop. Run from analysis/.
"""
import math, sys, os
from datetime import datetime, timezone
from collections import defaultdict
_o = sys.stdout; sys.stdout = open(os.devnull, "w")
import ma_pullback_chop as M          # its import builds M.ALL = [(ms,d,coin_fwd,btc_fwd,coin_er,btc_er)]
sys.stdout.close(); sys.stdout = _o

ALL = M.ALL; COST = M.COST
THRS = [0.0, 0.15, 0.20, 0.25, 0.30]
def mon(ms): return datetime.fromtimestamp(ms/1000, timezone.utc).strftime('%Y-%m')

def raw_net(rows):
    if not rows: return None
    v = [d*cf - COST for _, d, cf, bf, ec, be in rows]
    return sum(v)/len(v)

def choose_thr(train):
    """threshold maximising RAW net on train (naive optimiser); require >=100 trades to qualify."""
    best, bnet = 0.0, -1e9
    for thr in THRS:
        sel = [x for x in train if x[5] >= thr]
        if len(sel) < 100: continue
        net = raw_net(sel)
        if net is not None and net > bnet:
            best, bnet = thr, net
    return best

months = sorted(set(mon(x[0]) for x in ALL))
oos = []                       # pooled OOS filtered trades (each scored by a threshold that never saw its month)
rows_out = []
for Mn in months:
    train = [x for x in ALL if mon(x[0]) != Mn]
    test = [x for x in ALL if mon(x[0]) == Mn]
    thr = choose_thr(train)
    filt = [x for x in test if x[5] >= thr]
    oos += filt
    base_net = raw_net(test); filt_net = raw_net(filt)
    rows_out.append((Mn, thr, len(test), base_net, len(filt), filt_net))

def stats(rows, neutral):
    v = [(d*(cf-bf) if neutral else d*cf) - COST for _, d, cf, bf, ec, be in rows if (not neutral or bf is not None)]
    if len(v) < 5: return (float('nan'), float('nan'), 0)
    n = len(v); m = sum(v)/n; sd = (sum((x-m)**2 for x in v)/n)**0.5
    return m*1e4, (m/sd*math.sqrt(n) if sd > 0 else 0), n

print("Leave-one-month-out OOS of the BTC-ER chop filter (threshold picked on the OTHER months)\n")
print(f"  {'month':8s} {'thr':>4} {'baseN':>6} {'baseNet':>8}   {'keptN':>6} {'filtNet(OOS)':>12}")
for Mn, thr, bn, bnet, fn, fnet in rows_out:
    bs = f"{bnet*1e4:+8.1f}" if bnet is not None else "     n/a"
    fs = f"{fnet*1e4:+8.1f}" if fnet is not None else "     n/a"
    print(f"  {Mn:8s} {thr:>4.2f} {bn:>6} {bs}   {fn:>6} {fs}")

br, brt, bnn = stats(ALL, False);   bnr, bnrt, _ = stats(ALL, True)
fr, frt, fnn = stats(oos, False);   fnr, fnrt, _ = stats(oos, True)
print("\n  POOLED:")
print(f"    baseline (all, no filter)   n={bnn:5d}  RAW {br:+7.1f} (t{brt:+.1f})  NEUTRAL {bnr:+6.1f} (t{bnrt:+.1f})")
print(f"    OOS chop-filtered (LOMO)    n={fnn:5d}  RAW {fr:+7.1f} (t{frt:+.1f})  NEUTRAL {fnr:+6.1f} (t{fnrt:+.1f})")
print("\nEvery OOS trade was kept by a threshold chosen without seeing its month (incl. May). RAW improvement")
print("= better market-timing (beta); NEUTRAL is the alpha test. Small clustered samples -> t overstated.")
