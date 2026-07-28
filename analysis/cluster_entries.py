#!/usr/bin/env python3
"""Are clustered entries less profitable than isolated ones?

When a wide market move fires many breakouts at once, the bot opens several fades simultaneously —
mostly the SAME side (a market-wide sell-off => many fade-LONGs). Those are not independent bets;
they are one correlated bet that the broad move reverts. Question: does per-trade P&L fall as the
entry gets more crowded, and specifically as SAME-side crowding rises? If so, a rate limit / mix cap
is justified.

For each signal we count, causally (trailing window only, knowable at entry):
  same_W = # same-side signals entered in (t-W, t]  (incl. self; 1 = isolated)
  opp_W  = # opposite-side signals in the same window
Then bucket the baseline fade net (no stop, hold to backstop; wide_stop.simulate) by crowding, and
test two rules the user proposed: (1) a same-side RATE LIMIT (skip if >=cap same-side in trailing W),
(2) report the long/short MIX at entry to see if net-imbalance, not raw count, is what hurts.
Pure stdlib. 1h panel. Run from analysis/.
"""
import math, sys, os, bisect
from datetime import datetime, timezone
from collections import defaultdict
_o = sys.stdout; sys.stdout = open(os.devnull, "w")
import wide_stop as w
sys.stdout.close(); sys.stdout = _o

MAXH = w.MAXH; per = w.per_sym; COST = w.COST
base, _ = w.simulate(None, None)

# build trade list with side, net ret, entry ms, conviction (vratio), month
trades = []
for (sym, i, brk), ret in zip(w.signals, base):
    t, hi, lo, c, v, r = per[sym]
    med = sorted(v[i-24:i])[12] if i >= 24 else 1.0
    vratio = v[i]/med if med > 0 else 0.0
    side = -brk   # +1 fade-long (down-break), -1 fade-short (up-break)
    mo = datetime.fromtimestamp(t[i]/1000, timezone.utc).strftime("%Y-%m")
    trades.append(dict(ms=t[i], exit=t[i+MAXH], side=side, ret=ret, vr=vratio, mo=mo))
trades.sort(key=lambda x: x['ms'])
N = len(trades)
ent = [x['ms'] for x in trades]

HOURS = 3600*1000
def crowd(idx, W):
    """trailing same/opp side counts within (t-W, t], causal."""
    t0 = trades[idx]['ms']; s = trades[idx]['side']
    lo = bisect.bisect_left(ent, t0 - W)
    same = opp = 0
    for j in range(lo, idx+1):
        if trades[j]['side'] == s: same += 1
        else: opp += 1
    return same, opp

def report_buckets(W, keyfn, labels, title):
    grp = defaultdict(list)
    for idx in range(N):
        grp[keyfn(idx, W)].append(trades[idx]['ret'])
    print(f"  {title} (trailing {W//HOURS}h window):")
    for k in labels:
        rs = grp.get(k, [])
        if not rs: continue
        n = len(rs); m = sum(rs)/n
        sd = (sum((x-m)**2 for x in rs)/n)**0.5 if n > 1 else 0
        win = sum(1 for x in rs if x > 0)/n*100
        tstat = m/sd*math.sqrt(n) if sd > 0 else 0
        print(f"    {str(k):10s} n={n:5d}  net={m*1e4:+7.1f}bps  win={win:4.1f}%  t={tstat:+5.2f}  cum={sum(rs)*100:+8.1f}%")

def same_bucket(idx, W):
    s, _ = crowd(idx, W)
    return s if s <= 3 else '4+'
def total_bucket(idx, W):
    s, o = crowd(idx, W); tot = s + o
    return tot if tot <= 3 else '4+'
def imbalance_bucket(idx, W):
    s, o = crowd(idx, W); net = s - o     # net same-side directional crowding
    return '1 (iso)' if s == 1 else ('bal' if net <= 1 else ('imb2-3' if net <= 3 else 'imb4+'))

print(f"clustered-entry diagnostic — {N} signals, baseline net = {sum(base)/N*1e4:+.1f} bps/trade\n")
for W in (1*HOURS, 3*HOURS, 6*HOURS):
    report_buckets(W, same_bucket, [1, 2, 3, '4+'], "by SAME-SIDE crowd")
    report_buckets(W, total_bucket, [1, 2, 3, '4+'], "by TOTAL crowd (both sides)")
    report_buckets(W, imbalance_bucket, ['1 (iso)', 'bal', 'imb2-3', 'imb4+'], "by directional IMBALANCE")
    print()

# ---- position in a burst: does the 1st entry beat later ones? ----
print("  Rank within a 3h same-side burst (1 = first to fire):")
W = 3*HOURS; grp = defaultdict(list)
for idx in range(N):
    s, _ = crowd(idx, W)
    grp[s if s <= 4 else '5+'].append(trades[idx]['ret'])
for k in [1, 2, 3, 4, '5+']:
    rs = grp.get(k, [])
    if rs:
        n = len(rs); m = sum(rs)/n
        print(f"    rank {str(k):3s}  n={n:5d}  net={m*1e4:+7.1f}bps")

# ---- RULE TEST: same-side rate limit (skip if >= cap same-side in trailing W) ----
def rate_limit(cap, W, by_conviction=False):
    kept_idx = []
    recent = defaultdict(list)   # side -> list of (ms) kept entries
    order = range(N)
    for idx in order:
        t0 = trades[idx]['ms']; s = trades[idx]['side']
        recent[s] = [m for m in recent[s] if m > t0 - W]
        if len(recent[s]) >= cap:
            continue
        kept_idx.append(idx); recent[s].append(t0)
    rk = [trades[i]['ret'] for i in kept_idx]
    n = len(rk); m = sum(rk)/n
    # drawdown & worst 48h on kept, in exit order
    ev = sorted((trades[i] for i in kept_idx), key=lambda x: x['exit'])
    cum = peak = mdd = 0.0
    for tr in ev:
        cum += 100*tr['ret']; peak = max(peak, cum); mdd = min(mdd, cum-peak)
    # worst 48h clustered loss (by entry)
    exits_sorted = sorted(kept_idx, key=lambda i: trades[i]['ms'])
    pk = [trades[i]['ms'] for i in exits_sorted]; worst48 = 0.0
    for a in range(len(exits_sorted)):
        hi = bisect.bisect_right(pk, pk[a] + 48*HOURS)
        s48 = sum(100*trades[exits_sorted[b]]['ret'] for b in range(a, hi))
        worst48 = min(worst48, s48)
    return dict(n=n, net=m*1e4, cum=cum, mdd=mdd, worst48=worst48, ret_dd=(cum/-mdd if mdd < 0 else float('inf')))

print("\n  RULE: same-side rate limit (max `cap` same-side entries per trailing window), flat $100:")
b0 = rate_limit(10**9, 1)  # baseline (no cap)
print(f"    {'rule':22s} {'n':>5} {'net bps':>8} {'cum$':>9} {'maxDD$':>9} {'worst48h$':>10} {'ret/DD':>7}")
print(f"    {'baseline (no cap)':22s} {b0['n']:>5} {b0['net']:>+8.1f} {b0['cum']:>+9.0f} {b0['mdd']:>+9.0f} {b0['worst48']:>+10.0f} {b0['ret_dd']:>7.2f}")
for W in (3*HOURS, 6*HOURS, 12*HOURS):
    for cap in (2, 3, 4):
        d = rate_limit(cap, W)
        print(f"    cap{cap}/{W//HOURS:>2}h              {d['n']:>5} {d['net']:>+8.1f} {d['cum']:>+9.0f} {d['mdd']:>+9.0f} {d['worst48']:>+10.0f} {d['ret_dd']:>7.2f}")
    print()
print("  net bps = per-trade edge on KEPT trades; ret/DD = total $ / max drawdown $ (higher=better).")
