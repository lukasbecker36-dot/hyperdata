#!/usr/bin/env python3
"""Does order-flow toxicity at entry predict the fade's outcome? (the VPIN payoff test)

The live arm shadow-logs three flow features on every fill (computed from the tape at signal time):
  vpin30, vpin60  -- volume-synchronised prob. of informed trading over the last 30 / 60 min
  adverse_ofi     -- signed order-flow imbalance in the fade's ADVERSE direction (for a short, net
                     buying that keeps lifting; the "is the breakout still being pushed?" signal)
Hypothesis worth money: high toxicity / high adverse flow at entry = informed continuation = the fade
runs to the backstop; low toxicity = an uninformed flush = it reverts. If true, gating entries (or sizing
down) on toxicity clips the -395bps tail that no PRICE or VOLUME signal could (see early_exit.py).

Reads the live trades CSV (default live_15m_ats_trades.csv). For each flow feature: outcome by quintile
(net bps, win%, backstop-rate), rank correlation with net_bps, and a "drop the most-toxic X%" P&L test.
Pure stdlib. Run:  python3 vpin_outcome.py [trades.csv]
"""
import csv, sys, math
from collections import defaultdict

PATH = sys.argv[1] if len(sys.argv) > 1 else "live_15m_ats_trades.csv"
FEATS = ["vpin30", "vpin60", "adverse_ofi"]

rows = []
with open(PATH) as f:
    for r in csv.DictReader(f):
        try:
            net = float(r["net_bps"]); reason = r.get("reason", "")
            feat = {}
            for k in FEATS:
                s = (r.get(k) or "").strip()
                feat[k] = float(s) if s not in ("", "None", "nan") else None
            rows.append(dict(net=net, pnl=float(r.get("pnl_usd") or 0), reason=reason, **feat))
        except Exception:
            pass

n = len(rows)
if n == 0:
    print(f"no parseable rows in {PATH}"); sys.exit(0)
base_net = sum(r["net"] for r in rows)/n
base_bs = sum(1 for r in rows if "backstop" in r["reason"])/n*100
print(f"{PATH}: {n} trades | baseline net {base_net:+.1f}bps, win {sum(1 for r in rows if r['net']>0)/n*100:.0f}%, "
      f"backstop-rate {base_bs:.0f}%\n")

def spearman(pairs):
    m = len(pairs)
    def ranks(vals):
        order = sorted(range(m), key=lambda i: vals[i]); rk = [0.0]*m; i = 0
        while i < m:
            j = i
            while j+1 < m and vals[order[j+1]] == vals[order[i]]: j += 1
            avg = (i+j)/2.0 + 1
            for k in range(i, j+1): rk[order[k]] = avg
            i = j+1
        return rk
    xr = ranks([p[0] for p in pairs]); yr = ranks([p[1] for p in pairs])
    mx = sum(xr)/m; my = sum(yr)/m
    num = sum((xr[i]-mx)*(yr[i]-my) for i in range(m))
    den = (sum((xr[i]-mx)**2 for i in range(m))*sum((yr[i]-my)**2 for i in range(m)))**0.5
    return num/den if den else 0.0

for k in FEATS:
    have = [r for r in rows if r[k] is not None]
    if len(have) < 25:
        print(f"=== {k}: only {len(have)} populated rows — too thin (tape may not cover these trades) ===\n")
        continue
    have.sort(key=lambda r: r[k])
    m = len(have); q = m//5
    rho = spearman([(r[k], r["net"]) for r in have])
    print(f"=== {k}  ({m} populated)  rank-corr with net_bps: {rho:+.2f} ===")
    for b in range(5):
        seg = have[b*q:(b+1)*q] if b < 4 else have[4*q:]
        mn = sum(r["net"] for r in seg)/len(seg)
        bs = sum(1 for r in seg if "backstop" in r["reason"])/len(seg)*100
        wr = sum(1 for r in seg if r["net"] > 0)/len(seg)*100
        print(f"   Q{b+1}  {k}~{sum(r[k] for r in seg)/len(seg):+7.3f}  net {mn:+7.1f}bps  win {wr:3.0f}%  backstop {bs:3.0f}%")
    # drop-the-toxic-X% P&L test (high feature = more toxic; drop the top X%)
    print("   drop most-toxic:", end="")
    for frac in (0.1, 0.2, 0.3):
        keep = have[:int(m*(1-frac))]
        print(f"  -{int(frac*100)}%: net {sum(r['net'] for r in keep)/len(keep):+.1f}b (n={len(keep)})", end="")
    print("\n")
print("high feature = more toxic/informed. A monotone DROP in net across Q1->Q5 (and a positive 'drop-toxic'")
print("lift) would mean toxicity gates the tail. rank-corr near 0 / non-monotone = no usable signal.")
