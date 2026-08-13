#!/usr/bin/env python3
"""Late conditional exit: bail when the reversion isn't coming and the runway is short.

Distinct from early_exit.py (volume in the first 1-2 bars -> backfired: high vol = bigger reversion).
This conditions on the LATE state of the trade: still open (not reclaimed), still underwater, still being
pushed away, with only k_left bars to the 8h backstop. The question: for a trade in that state, does the
remaining hold have negative expected value (so bailing at market beats waiting for the backstop)?

Causal: everything measured from bars i+1..i+k, decision taken at bar k. Baseline = hold to reclaim /
8h backstop. Rules bail at c[i+k] when late + adverse (+ optional volume-surge / still-making-new-lows).
Reports total P&L, backstop-loser $ avoided, winner $ cut, and return/|DD| vs baseline. 1h panel. Run from analysis/.
"""
import math, sys, os
from collections import defaultdict
_o = sys.stdout; sys.stdout = open(os.devnull, "w")
import wide_stop as w
sys.stdout.close(); sys.stdout = _o

per = w.per_sym; COST = w.COST; MAXH = w.MAXH; WIN = 24
def med(xs): s = sorted(xs); n = len(s); return s[n//2] if n % 2 else 0.5*(s[n//2-1]+s[n//2])

# build full path per trade
ev = []
for sym, i, brk in w.signals:
    t, hi, lo, c, v, ret = per[sym]
    if i+MAXH >= len(c): continue
    d = -brk; e = c[i]; ph = max(hi[i-WIN:i]); pl = min(lo[i-WIN:i]); mv = med(v[i-WIN:i]) or 1.0
    path = []
    reclaim_k = None
    worst = 0.0
    for k in range(1, MAXH+1):
        cc = c[i+k]
        adv = d*math.log(cc/e)                     # <0 = losing
        worst = min(worst, adv)
        new_low = adv <= worst + 1e-12             # at/at-new adverse extreme
        vsurge = v[i+k]/mv
        rec = (d < 0 and cc < ph) or (d > 0 and cc > pl)
        path.append(dict(k=k, cc=cc, adv=adv, vsurge=vsurge, new_low=new_low, rec=rec))
        if rec and reclaim_k is None: reclaim_k = k
    # baseline exit
    bx = reclaim_k if reclaim_k else MAXH
    ev.append(dict(d=d, e=e, path=path, base_net=d*math.log(c[i+bx]/e)-COST,
                   base_reason='reclaim' if reclaim_k else 'backstop', reclaim_k=reclaim_k))
N = len(ev); base_tot = sum(x['base_net'] for x in ev)
def dd(nets_by_exit):
    cum = pk = m = 0.0
    for p in nets_by_exit: cum += p; pk = max(pk, cum); m = min(m, cum-pk)
    return m
base_dd = dd([x['base_net'] for x in ev])
print(f"{N} trades | baseline: net {base_tot/N*1e4:+.1f}bps, total {base_tot*100:+.0f}%, "
      f"maxDD {base_dd*100:+.0f}%, backstop-rate {sum(1 for x in ev if not x['reclaim_k'])/N*100:.0f}%\n")

def rule(kmin, adv_thr, need_vol, need_newlow, vmult=5.0):
    """bail at first bar k>=kmin where (not yet reclaimed) and adverse< -adv_thr and conditions."""
    nets = []; bailed = 0; saved = 0.0; cut = 0.0
    for x in ev:
        exit_net = None
        for p in x['path']:
            if p['rec']:                            # reclaim wins first
                exit_net = x['d']*0  # placeholder
                exit_net = p['adv'] - COST; break   # adv at reclaim bar = the reclaim P&L
            if (p['k'] >= kmin and p['adv'] < -adv_thr
                    and (not need_vol or p['vsurge'] >= vmult)
                    and (not need_newlow or p['new_low'])):
                exit_net = p['adv'] - COST          # bail at this bar's close
                bailed += 1
                saved += x['base_net'] - exit_net   # +ve = we improved vs baseline
                break
        if exit_net is None: exit_net = x['base_net']
        nets.append(exit_net)
    tot = sum(nets)
    return dict(n=bailed, tot=tot, delta=(tot-base_tot), dd=dd(nets), rdd=tot/abs(dd(nets)) if dd(nets) else 0)

print(f"  {'rule':38s} {'bailed':>6} {'total%':>8} {'vs base':>8} {'maxDD%':>8} {'ret/DD':>7}")
print(f"  {'baseline (hold to reclaim/backstop)':38s} {'-':>6} {base_tot*100:>+8.0f} {'-':>8} {base_dd*100:>+8.0f} {base_tot/abs(base_dd):>7.2f}")
for kmin in (4, 6, 7):
    kl = MAXH-kmin
    for adv in (0.02, 0.04):
        for nv, nn, tag in ((False, False, "adverse only"), (True, False, "adv+vol-surge"),
                            (False, True, "adv+still-falling"), (True, True, "adv+vol+falling")):
            r = rule(kmin, adv, nv, nn)
            print(f"  k>={kmin} ({kl}h left), <-{adv*100:.0f}%, {tag:18s} {r['n']:>6} "
                  f"{r['tot']*100:>+8.0f} {r['delta']*100:>+8.0f} {r['dd']*100:>+8.0f} {r['rdd']:>7.2f}")
    print()
print("vs base > 0 = the rule beats holding to backstop. The fade thesis says late reversion still comes,")
print("so expect these to be <=0; a positive one would be a genuine dynamic stop that price/ATR stops are not.")
