#!/usr/bin/env python3
"""Flow toxicity on 273 live fills: is there anything actionable in it yet?

vpin30/vpin60/adverse_ofi have been recorded at SIGNAL time and never acted on since
2026-07-26. That makes them genuinely causal features -- unlike the post-entry volume in
volume_persist.py, they cannot know their own outcome, and unlike toxicity.py's original
label they are not a restatement of the result. So the usual endogeneity objection does
not apply here, and the question is only whether they carry information.

  vpin        |buy - sell| / (buy + sell) over the trailing 30 or 60 minutes. High = flow
              is one-sided = someone is pushing, which is the classic marker of informed
              trading. A fade wants two-sided churn, not one-sided pressure.
  adverse_ofi the same imbalance SIGNED by the breakout direction, so positive means the
              flow is running INTO the position we are about to take. This is the more
              specific hypothesis: not "is flow toxic" but "is it toxic to US".

PASS CRITERIA, fixed before looking, because on this dataset five separate filters have
produced a good headline and then failed exactly one of these:

  1. MONOTONE     the gradient runs one way across quintiles, not one hot bucket
  2. BOTH HALVES  present in the first and second half of the sample, same sign
  3. UNCONCENTRATED  top-3 trades are not most of the dollars, and no single week is
  4. NOT REDUNDANT   survives controlling for ats_ratio, which is already deployed
  5. HONEST t     3 features x ~4 cuts is ~12 looks, so t=+2 is expected by chance;
                  the bar is |t| >= 3 or nothing

  python3 analysis/vpin_edge.py [trades.csv] [misses.csv]
"""
import csv, math, sys
from collections import defaultdict
from datetime import datetime

TR = sys.argv[1] if len(sys.argv) > 1 else "live_15m_ats/trades_15m.csv"
MS = sys.argv[2] if len(sys.argv) > 2 else "live_15m_ats/missed_15m_ats.csv"
FEATS = ("vpin30", "vpin60", "adverse_ofi")

rows = []
for r in csv.DictReader(open(TR)):
    try:
        net = float(r["net_bps"])
        if abs(net) < 1e-9 or not (r.get("vpin30") or "").strip():
            continue
        d = dict(sym=r["symbol"], net=net, pnl=float(r["pnl_usd"]), reason=r["reason"],
                 tier=(r.get("tier") or "").strip(),
                 t=datetime.strptime(r["entry_time"], "%Y-%m-%d %H:%M:%S"))
        for k in FEATS + ("ats_ratio", "spread_bps"):
            v = (r.get(k) or "").strip()
            d[k] = float(v) if v else None
        rows.append(d)
    except Exception:
        pass
rows.sort(key=lambda r: r["t"])
N = len(rows)
misses = []
try:
    for r in csv.DictReader(open(MS)):
        if all((r.get(k) or "").strip() for k in FEATS):
            misses.append({k: float(r[k]) for k in FEATS})
except FileNotFoundError:
    pass
print(f"{N} fills carry flow features, {len(misses)} misses do, "
      f"{rows[0]['t']:%Y-%m-%d} to {rows[-1]['t']:%Y-%m-%d}\n")


def st(v):
    n = len(v)
    if n < 2:
        return (float("nan"), float("nan"), n)
    mu = sum(v) / n
    sd = (sum((x - mu) ** 2 for x in v) / (n - 1)) ** 0.5
    return (mu, mu / (sd / math.sqrt(n)) if sd > 0 else float("nan"), n)


def conc(seg):
    tot = sum(r["pnl"] for r in seg)
    if abs(tot) < 1e-9:
        return float("nan")
    return 100 * sum(r["pnl"] for r in sorted(seg, key=lambda r: -abs(r["pnl"]))[:3]) / tot


def corr(a, b):
    n = len(a)
    ma, mb = sum(a)/n, sum(b)/n
    sa = math.sqrt(sum((x-ma)**2 for x in a)/(n-1))
    sb = math.sqrt(sum((x-mb)**2 for x in b)/(n-1))
    if sa <= 0 or sb <= 0:
        return float("nan"), float("nan")
    r = sum((a[i]-ma)*(b[i]-mb) for i in range(n))/((n-1)*sa*sb)
    return r, r*math.sqrt((n-2)/max(1e-12, 1-r*r))


print("=== 1. MONOTONICITY: quintiles of each feature ===")
qres = {}
for f in FEATS:
    seg = sorted([r for r in rows if r[f] is not None], key=lambda r: r[f])
    n = len(seg); k = n // 5
    print(f"--- {f} ---")
    print(f"  {'quintile':>22} {'n':>5} {'bps':>9} {'t':>6} {'win%':>6} "
          f"{'backstop':>9} {'blowup':>7} {'top3':>8}")
    means = []
    for i in range(5):
        s = seg[i*k:(i+1)*k if i < 4 else n]
        b = [r["net"] for r in s]
        mu, t, kk = st(b)
        means.append(mu)
        nb = sum(1 for r in s if r["reason"].startswith("backstop"))
        print(f"  {f'Q{i+1} {s[0][f]:+.2f} to {s[-1][f]:+.2f}':>22} {kk:>5} {mu:>+9.1f} "
              f"{t:>+6.1f} {100*sum(1 for x in b if x>0)/kk:>5.0f}% {100*nb/kk:>8.0f}% "
              f"{100*sum(1 for x in b if x<-400)/kk:>6.0f}% {conc(s):>+7.0f}%")
    ups = sum(1 for i in range(4) if means[i+1] > means[i])
    print(f"  monotone steps: {ups}/4 up  -> {'MONOTONE' if ups in (0,4) else 'NOT monotone'}")
    r, t = corr([x[f] for x in seg], [x["net"] for x in seg])
    print(f"  corr({f}, net_bps) = {r:+.3f}  t={t:+.2f}\n")
    qres[f] = (seg, means)

print("=== 2. BOTH HALVES: does the top-vs-bottom gap repeat out of sample? ===")
half = rows[N//2]["t"]
print(f"  split at {half:%Y-%m-%d %H:%M}")
print(f"  {'feature':>14} {'half':>8} {'low Q1-Q2':>11} {'high Q4-Q5':>12} {'gap':>9} {'n':>10}")
for f in FEATS:
    seg, _ = qres[f]
    n = len(seg); k = n // 5
    lo_set = {id(x) for x in seg[:2*k]}
    hi_set = {id(x) for x in seg[3*k:]}
    for lab, sub in (("first", [r for r in rows if r["t"] <= half]),
                     ("second", [r for r in rows if r["t"] > half])):
        lo = [r["net"] for r in sub if id(r) in lo_set]
        hi = [r["net"] for r in sub if id(r) in hi_set]
        if len(lo) < 10 or len(hi) < 10:
            continue
        a, b = sum(lo)/len(lo), sum(hi)/len(hi)
        print(f"  {f:>14} {lab:>8} {a:>+11.1f} {b:>+12.1f} {b-a:>+9.1f} "
              f"{f'{len(lo)}/{len(hi)}':>10}")
print()

print("=== 3. NOT REDUNDANT: are these just restating ats_ratio or spread? ===")
base = [r for r in rows if r["ats_ratio"] is not None and r["spread_bps"] is not None]
print(f"  {'':>14} " + " ".join(f"{c:>14}" for c in ("ats_ratio", "spread_bps", "net_bps")))
for f in FEATS:
    cells = []
    for c in ("ats_ratio", "spread_bps", "net"):
        r, t = corr([x[f] for x in base], [x[c] for x in base])
        cells.append(f"{r:+.3f} (t{t:+.1f})")
    print(f"  {f:>14} " + " ".join(f"{c:>14}" for c in cells))
print(f"  n={len(base)}. A flow feature strongly correlated with ats_ratio is not new")
print("  information -- ats sizing is already live and would double-count it.\n")

print("=== 4. THE OBVIOUS FILTERS, PRICED ===")
tot = sum(r["pnl"] for r in rows)
mu0, t0, _ = st([r["net"] for r in rows])
print(f"  {'rule':>34} {'kept':>6} {'bps':>9} {'t':>6} {'$ total':>9} {'vs base':>9} "
      f"{'blowups':>8}")
print(f"  {'no filter':>34} {N:>6} {mu0:>+9.1f} {t0:>+6.1f} {tot:>+9.2f} {0:>+9.2f} "
      f"{sum(1 for r in rows if r['net']<-400):>8}")
cands = []
for f in FEATS:
    seg, _ = qres[f]
    n = len(seg); k = n // 5
    for lab, keep in ((f"skip top quintile {f}", seg[:4*k]),
                      (f"skip bottom quintile {f}", seg[k:])):
        b = [r["net"] for r in keep]
        mu, t, kk = st(b)
        s = sum(r["pnl"] for r in keep)
        print(f"  {lab:>34} {kk:>6} {mu:>+9.1f} {t:>+6.1f} {s:>+9.2f} {s-tot:>+9.2f} "
              f"{sum(1 for r in keep if r['net']<-400):>8}")
        cands.append((lab, keep, s - tot))

print("\n=== 5. CONCENTRATION BY WEEK of the best-looking filter ===")
best = max(cands, key=lambda c: c[2])
print(f"  best rule: {best[0]} ({best[2]:+.2f} vs no filter)")
dropped = [r for r in rows if r not in best[1]]
byw = defaultdict(float)
for r in dropped:
    byw[r["t"].strftime("%Y-W%W")] -= r["pnl"]
s = sum(byw.values())
for w in sorted(byw):
    print(f"    {w}  {byw[w]:>+7.2f}  ({100*byw[w]/s if s else float('nan'):>+5.0f}% of the gain)")

print("\n=== 6. FILL RATE: does toxic flow predict whether a resting order fills? ===")
if misses:
    for f in FEATS:
        fv = [r[f] for r in rows if r[f] is not None]
        mv = [m[f] for m in misses]
        a, b = sum(fv)/len(fv), sum(mv)/len(mv)
        sa = (sum((x-a)**2 for x in fv)/(len(fv)-1))**.5
        sb = (sum((x-b)**2 for x in mv)/(len(mv)-1))**.5
        se = math.sqrt(sa*sa/len(fv) + sb*sb/len(mv))
        print(f"  {f:>14}  filled {a:>+7.3f}  missed {b:>+7.3f}  diff {a-b:>+7.3f}  "
              f"t={(a-b)/se if se>0 else float('nan'):>+5.2f}")
print("\n  Reminder of the bar: monotone, both halves, unconcentrated, not redundant,")
print("  and |t| >= 3. Twelve looks at 273 trades will hand you a t=+2 for free.")
