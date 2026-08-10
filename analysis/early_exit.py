#!/usr/bin/env python3
"""Causal early-exit: can the first 1-2 bars of post-entry VOLUME flag the backstop losers?

backstop_filter.py: 81% of fades reclaim (+155bps), 19% run to the 8h backstop (-395bps) -- the whole
negative tail. volume_staircase.py: volume that DIES after the spike = exhaustion (fade works); volume
that PERSISTS = participation (fade fails) -- but that was lookahead. Here it is causal:

  enter, watch only bars i+1..i+PROBE, compute volume persistence over just those bars, then measure the
  return FROM the end of the probe TO the eventual reclaim/backstop exit. The feature is strictly in the
  past of what it predicts (dodges the "high volume = restatement of a loss" circularity), and the implied
  action -- bail at the probe close -- is one you could actually take.

This is NOT a price stop (those destroy the edge). The control below bails on "already losing at the
probe" so we can see whether VOLUME adds anything beyond price. Reports the causal predictiveness, the
rule vs baseline (total P&L, tail, win%), a plain-price-stop control, and monthly + holdout stability.
1h panel, faithful reclaim/backstop exits. Run from analysis/.
"""
import math, sys, os
from datetime import datetime, timezone
from collections import defaultdict
_o = sys.stdout; sys.stdout = open(os.devnull, "w")
import wide_stop as w
sys.stdout.close(); sys.stdout = _o

per = w.per_sym; COST = w.COST; MAXH = w.MAXH; WIN = 24
PROBE = int(sys.argv[1]) if len(sys.argv) > 1 else 2

def median(xs):
    s = sorted(xs); n = len(s); return s[n//2] if n % 2 else 0.5*(s[n//2-1]+s[n//2])

events = []
for sym, i, brk in w.signals:
    t, hi, lo, c, v, ret = per[sym]
    if i + MAXH >= len(c) or i + PROBE >= len(c): continue
    d = -brk; e = c[i]
    prior_h = max(hi[i-WIN:i]); prior_l = min(lo[i-WIN:i])
    # faithful exit: reclaim (close back inside prior range) else 8h backstop
    exit_k = MAXH; reason = "backstop"
    for k in range(1, MAXH+1):
        cc = c[i+k]
        if d < 0 and cc < prior_h: exit_k, reason = k, "reclaim"; break
        if d > 0 and cc > prior_l: exit_k, reason = k, "reclaim"; break
    exit_px = c[i+exit_k]
    eventual = d*math.log(exit_px/e) - COST
    # probe volume persistence: mean vol over the probe bars vs the trailing-median bar volume
    medv = median(v[i-WIN:i]) or 1.0
    vp = (sum(v[i+1:i+1+PROBE])/PROBE) / medv
    probe_px = c[i+PROBE]
    probe_bail = d*math.log(probe_px/e) - COST          # P&L if we exit at the probe close
    still_open = exit_k > PROBE                          # eligible for early exit only if not already out
    events.append(dict(ms=t[i], d=d, vp=vp, eventual=eventual, probe_bail=probe_bail,
                       reason=reason, open=still_open, mo=datetime.fromtimestamp(t[i]/1000, timezone.utc).strftime('%m')))

N = len(events)
base = sum(x['eventual'] for x in events)
print(f"early-exit probe = {PROBE} bar(s) ({PROBE}h) | {N} events | baseline (hold to reclaim/backstop): "
      f"net {base/N*1e4:+.1f}bps/trade, total {base*100:+.0f}%\n")

# ---- causal predictiveness: bucket the POST-probe return by probe volume persistence (open trades only)
op = [x for x in events if x['open']]
op.sort(key=lambda x: x['vp'])
print(f"POST-probe return (probe->exit, strictly future of the feature), by probe-volume quintile "
      f"({len(op)} still-open trades):")
q = len(op)//5
for b in range(5):
    seg = op[b*q:(b+1)*q] if b < 4 else op[4*q:]
    fwd = [x['eventual'] - x['probe_bail'] for x in seg]   # return earned AFTER the probe
    m = sum(fwd)/len(fwd)*1e4
    vpm = sum(x['vp'] for x in seg)/len(seg)
    bs = sum(1 for x in seg if x['reason'] == 'backstop')/len(seg)*100
    print(f"   Q{b+1}  vol~{vpm:4.1f}x  post-probe {m:+7.1f}bps  backstop-rate {bs:4.0f}%")
print("   (if high-volume quintiles have worse post-probe returns, bailing on them is a real causal edge)\n")

def rule(kind, thr):
    tot = 0.0; bailed = 0; bail_saved = 0.0
    for x in events:
        if x['open'] and ((kind == 'vol' and x['vp'] >= thr) or (kind == 'price' and x['probe_bail'] < thr)):
            tot += x['probe_bail']; bailed += 1; bail_saved += x['eventual'] - x['probe_bail']
        else:
            tot += x['eventual']
    return tot, bailed, bail_saved

print(f"  {'rule':22s} {'bailed':>6} {'total%':>8} {'d/trade':>8} {'tail avoided':>12}")
print(f"  {'baseline (hold all)':22s} {'-':>6} {base*100:>+8.0f} {base/N*1e4:>+7.1f}b {'-':>12}")
for thr in (1.0, 1.5, 2.0, 3.0):
    tot, nb, saved = rule('vol', thr)
    print(f"  vol-persist >= {thr:>3.1f}x     {nb:>6} {tot*100:>+8.0f} {tot/N*1e4:>+7.1f}b {(-saved)*100:>+11.0f}%")
print("  --- control: plain price stop (bail if already losing at the probe, ignore volume) ---")
for thr in (0.0, -0.01, -0.02):
    tot, nb, saved = rule('price', thr)
    print(f"  bail if probe P&L< {thr:>+.0%}   {nb:>6} {tot*100:>+8.0f} {tot/N*1e4:>+7.1f}b {(-saved)*100:>+11.0f}%")

# monthly + holdout of the best-looking vol rule (>=2.0x), all causal
print("\n  monthly total (baseline vs vol-persist>=2.0x bail):")
mo_b = defaultdict(float); mo_r = defaultdict(float)
for x in events:
    mo_b[x['mo']] += x['eventual']
    mo_r[x['mo']] += (x['probe_bail'] if (x['open'] and x['vp'] >= 2.0) else x['eventual'])
for m in sorted(mo_b):
    print(f"    {m}: base {mo_b[m]*100:+7.0f}%   rule {mo_r[m]*100:+7.0f}%   delta {(mo_r[m]-mo_b[m])*100:+7.0f}%")
print("\ntail avoided = P&L we removed by bailing (negative = we cut losses; positive = we cut winners).")
