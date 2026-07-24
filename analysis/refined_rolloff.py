#!/usr/bin/env python3
"""REFINED 24h % display-artifact roll-off.

The naive version (rolloff_24h.py) ranked coins by the raw return of the candle 24h ago and
failed its placebo (lag=12 beat lag=24) => it was just generic short-horizon reversal, not an
artifact. This refines the idea to target the actual mechanism and strip the reversal contamination:

  1. LEADERBOARD SALIENCE: only trade coins whose CURRENT displayed 24h% is an extreme AND that
     extreme is CAUSED by the about-to-roll-off candle (sign(r_roll)==sign(disp) and |r_roll| is a
     meaningful share of |disp|). That is exactly the ticker number retail watches mechanically move.
  2. MAGNITUDE GATE: bigger rolling-off candle -> bigger display jump.
  3. ORTHOGONALIZE vs recent R-hour return: remove the generic reversal the placebo exposed, so the
     signal is the STALE 24h-ago candle's surprise, not last-few-hours mean reversion.
  4. Short hold (artifact is a one-time roll event), precise hourly roll timing.

Direction: ride the artifact flow — LONG biggest-red-rolling-off (display about to rise -> naive buy),
SHORT biggest-green-rolling-off (display about to drop -> naive sell). Market-neutral, non-overlapping.
Then re-run the lag=12 PLACEBO on the SAME refined pipeline: if the refinement is a real 24h artifact,
lag=24 must now beat lag=12. 1h panel (full 8mo). Run from analysis/.
"""
import math, sys, os
from collections import defaultdict
# silence wide_stop's import-time self-report
_o = sys.stdout; sys.stdout = open(os.devnull, "w")
import wide_stop as w
sys.stdout.close(); sys.stdout = _o

HOLDOUT_DAYS = 45
R = 6                      # recent window (h) to orthogonalize against
per = w.per_sym
def moments(xs):
    n = len(xs); m = sum(xs)/n; sd = (sum((x-m)**2 for x in xs)/n)**0.5; return m, sd
liquid = [s for s in per if w.tier(w.uni.get(s, 0)) in ('HIGH', 'MID')]
maps = {s: {ms: k for k, ms in enumerate(per[s][0])} for s in liquid}
grid = per['BTC'][0] if 'BTC' in per else max((per[s] for s in liquid), key=lambda x: len(x[0]))[0]

def strat(H, lag=24, dec=0.1, share=0.5, mag_gate=0.5, salience=True, orth=True):
    """dec = decile fraction each side; share = min |r_roll|/|disp| to count as 'caused by' the roll;
    mag_gate = keep only coins in the top (1-mag_gate) by |r_roll| this hour; salience/orth toggles."""
    ser = []
    for g in range(lag+1, len(grid)-H, H):
        ms = grid[g]; cand = []
        for s in liquid:
            k = maps[s].get(ms)
            if k is None or k-lag-1 < 0 or k-R < 0 or k+H >= len(per[s][3]): continue
            c = per[s][3]
            r_roll = math.log(c[k-lag]/c[k-lag-1])       # candle rolling off
            disp   = math.log(c[k]/c[k-lag])             # currently displayed ~24h change
            recent = math.log(c[k]/c[k-R])               # recent momentum (reversal contaminant)
            fwd    = math.log(c[k+H]/c[k])
            cand.append([s, r_roll, disp, recent, fwd])
        if len(cand) < 20: continue

        # (2) magnitude gate: keep only the biggest rolling-off candles
        if mag_gate > 0:
            cand.sort(key=lambda x: abs(x[1]), reverse=True)
            cand = cand[:max(20, int(len(cand)*(1-mag_gate)))]

        # (1) salience: the roll candle must be inflating the displayed extreme in the same direction
        if salience:
            cand = [x for x in cand
                    if (x[1] >= 0) == (x[2] >= 0) and abs(x[1]) >= share*abs(x[2]) and abs(x[2]) > 1e-9]
        if len(cand) < 12: continue

        # (3) orthogonalize r_roll vs recent across the cross-section (strip generic reversal)
        if orth:
            xs = [x[3] for x in cand]; ys = [x[1] for x in cand]
            mx = sum(xs)/len(xs); my = sum(ys)/len(ys)
            sxx = sum((x-mx)**2 for x in xs); sxy = sum((xs[i]-mx)*(ys[i]-my) for i in range(len(xs)))
            b = sxy/sxx if sxx > 1e-12 else 0.0
            for x in cand: x.append(x[1] - (my + b*(x[3]-mx)))   # residual r_roll
            key = lambda x: x[5]
        else:
            key = lambda x: x[1]

        cand.sort(key=key)
        nd = max(1, int(len(cand)*dec))
        reds = cand[:nd]; greens = cand[-nd:]         # long reds (lowest signal), short greens
        ser.append((ms, 0.5*(sum(r[4] for r in reds)/nd - sum(gv[4] for gv in greens)/nd)))
    return ser

def summ(ser, H, cost):
    if len(ser) < 5: return None
    r = [x-cost for _, x in ser]; m, sd = moments(r); ppy = 8760.0/H
    ann = m/sd*math.sqrt(ppy) if sd > 0 else 0
    tmax = max(t for t, _ in ser); ho = [x-cost for t, x in ser if t >= tmax-HOLDOUT_DAYS*86400000]
    annh = (moments(ho)[0]/moments(ho)[1]*math.sqrt(ppy)) if len(ho) >= 3 and moments(ho)[1] > 0 else float('nan')
    return len(ser), m*1e4, ann, annh

print("REFINED 24h ROLL-OFF (leaderboard-salient + magnitude-gated + reversal-orthogonalized)\n")
print(f"  {'hold':>5} {'rebals':>6} {'gross bps':>9} {'annSh@0':>8} {'@5bp':>7} {'@10bp':>7} {'hold@5':>7}")
for H in (1, 2, 3, 6):
    g = summ(strat(H, 24), H, 0.0)
    if not g: continue
    n5 = summ(strat(H, 24), H, 0.0005); n10 = summ(strat(H, 24), H, 0.0010)
    print(f"  {H:>4}h {g[0]:>6} {g[1]:>+9.2f} {g[2]:>+8.2f} {n5[2]:>+7.2f} {n10[2]:>+7.2f} {n5[3]:>+7.2f}")

print("\nABLATION @ 3h hold (which refinement, if any, matters):")
for tag, kw in (("raw (no refine)", dict(salience=False, orth=False, mag_gate=0, dec=0.2)),
                ("+magnitude gate", dict(salience=False, orth=False)),
                ("+salience", dict(orth=False)),
                ("+orthogonalize (full)", dict())):
    g = summ(strat(3, 24, **kw), 3, 0.0)
    if g: print(f"  {tag:24s} rebals={g[0]:>4}  gross={g[1]:>+7.2f}  annSh@0={g[2]:>+6.2f}  hold={g[3]:>+6.2f}")

print("\nPLACEBO on the FULL refined pipeline — key off the 12h-ago candle instead of 24h.")
print("If this is a real 24h display artifact, lag=24 must BEAT lag=12:")
for H in (1, 3, 6):
    for lag, t in ((24, 'lag=24'), (12, 'lag=12')):
        g = summ(strat(H, lag), H, 0.0)
        if g: print(f"  {H:>4}h {t}  rebals={g[0]:>4}  gross={g[1]:>+7.2f}  annSh={g[2]:>+6.2f}")

print("\ngross = long-short spread per rebalance (bps).")
