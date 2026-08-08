#!/usr/bin/env python3
"""The wide-spread effect, tested on trades that did not exist when it was rejected.

On 2026-08-05, at n=177, live_spread.py rejected this signal. The stated reasons were
non-monotone quintiles, a reversal inside the MID tier, CASHCAT carrying 56% of the
dollars, and decay from +111.8bps in the first half to +37.1 in the second.

That rejection is a dated, falsifiable prediction: the next trades should be flat. 60
trades have closed since, chosen by a policy that knew nothing about the test. This is
the cleanest holdout available anywhere in this repo -- no reconstruction, no shuffle,
no in-sample ranking. Either the effect shows up in the new data or it does not.

The pre-registered call was FLAT. Report what actually happened, including the parts
that argue against the original rejection.

  python3 analysis/live_spread_holdout.py [trades.csv]
"""
import csv, math, sys
from collections import defaultdict
from datetime import datetime

PATH = sys.argv[1] if len(sys.argv) > 1 else "live_15m_ats/trades_15m.csv"
CUT = datetime(2026, 8, 5, 9, 44, 0)     # when the rejection was committed (76cc345/f118013)

rows = []
for r in csv.DictReader(open(PATH)):
    try:
        net = float(r["net_bps"])
        sp = (r.get("spread_bps") or "").strip()
        if abs(net) < 1e-9 or not sp:
            continue
        rows.append(dict(sym=r["symbol"], net=net, pnl=float(r["pnl_usd"]),
                         sp=float(sp), tier=(r.get("tier") or "").strip(),
                         reason=r["reason"], crossed=int(float(r["crossed"] or 0)),
                         t=datetime.strptime(r["entry_time"], "%Y-%m-%d %H:%M:%S")))
    except Exception:
        pass
rows.sort(key=lambda r: r["t"])
IN = [r for r in rows if r["t"] < CUT]
OUT = [r for r in rows if r["t"] >= CUT]
print(f"in-sample (the data the rejection was made on): {len(IN)}")
print(f"holdout   (closed since {CUT:%Y-%m-%d %H:%M}):        {len(OUT)}\n")


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


def line(lab, seg, w=26):
    if len(seg) < 3:
        print(f"  {lab:>{w}} n={len(seg):<4} --")
        return
    mu, t, n = st([r["net"] for r in seg])
    print(f"  {lab:>{w}} n={n:<4} {mu:>+8.1f} bps  t={t:>+5.1f}  "
          f"win {100*sum(1 for r in seg if r['net']>0)/n:>3.0f}%  "
          f"${sum(r['pnl'] for r in seg):>+7.2f}  top3 {conc(seg):>+6.0f}%")


print("=== 1. THE PREDICTION: the holdout should be flat ===")
for lab, seg in (("IN-SAMPLE", IN), ("HOLDOUT", OUT), ("COMBINED", rows)):
    print(f"  --- {lab} ---")
    line("wide  >5bps", [r for r in seg if r["sp"] > 5])
    line("tight <=5bps", [r for r in seg if r["sp"] <= 5])
    w = st([r["net"] for r in seg if r["sp"] > 5])
    t_ = st([r["net"] for r in seg if r["sp"] <= 5])
    if w[2] > 2 and t_[2] > 2:
        print(f"  {'spread of the spread':>26} {w[0]-t_[0]:>+8.1f} bps")
print()

print("=== 2. THE FOUR REASONS IT WAS REJECTED, re-checked on all data ===")
print("  (a) NON-MONOTONE QUINTILES -- one hot bucket with noise either side")
qs = sorted(r["sp"] for r in rows)
edges = [qs[int(len(qs) * f)] for f in (0.2, 0.4, 0.6, 0.8)]
prev = -1.0
for i, e in enumerate(edges + [1e9]):
    line(f"Q{i+1} {max(prev,0):.1f}-{e if e<1e9 else 999:.1f}bps",
         [r for r in rows if prev < r["sp"] <= e])
    prev = e

print("\n  (b) REVERSAL INSIDE THE MID TIER")
for tn in ("HIGH", "MID"):
    seg = [r for r in rows if r["tier"] == tn]
    if len(seg) < 10:
        continue
    print(f"    tier {tn} (n={len(seg)}):")
    line("wide  >5bps", [r for r in seg if r["sp"] > 5])
    line("tight <=5bps", [r for r in seg if r["sp"] <= 5])

print("\n  (c) CASHCAT CARRIED IT")
W = [r for r in rows if r["sp"] > 5]
byc = defaultdict(list)
for r in W:
    byc[r["sym"]].append(r)
tot = sum(r["pnl"] for r in W)
top = sorted(byc, key=lambda s: -sum(x["pnl"] for x in byc[s]))[:3]
print("    top 3 coins: " + ", ".join(
    f"{s} ${sum(x['pnl'] for x in byc[s]):+.2f}/{len(byc[s])}" for s in top))
line("wide, ex-top-3-coins", [r for r in W if r["sym"] not in top])
line("wide, ex-CASHCAT", [r for r in W if r["sym"] != "CASHCAT"])

print("\n  (d) DECAY OVER TIME -- thirds of the sample by entry time")
k = len(W) // 3
for i, lab in enumerate(("wide, first third", "wide, middle third", "wide, last third")):
    line(lab, W[i*k:(i+1)*k if i < 2 else len(W)])

print("\n=== 3. MECHANISM: is it fill selection or trade selection? ===")
print("  the bot rests when spread >5bps and crosses otherwise, so 'wide' and 'rested'")
print("  are nearly the same trades. Half the spread bounds the execution contribution:")
for lab, seg in (("wide (>5bps)", W), ("tight (<=5bps)", [r for r in rows if r["sp"] <= 5])):
    msp = sum(r["sp"] for r in seg) / len(seg)
    nb = sum(1 for r in seg if r["reason"].startswith("backstop"))
    print(f"  {lab:>16} mean spread {msp:>5.1f}bps (half = {msp/2:>4.1f})  "
          f"backstop rate {100*nb/len(seg):>4.0f}%  rested {sum(1 for r in seg if not r['crossed'])}/{len(seg)}")
dn = st([r["net"] for r in W])[0] - st([r["net"] for r in rows if r["sp"] <= 5])[0]
dh = (sum(r["sp"] for r in W)/len(W)
      - sum(r["sp"] for r in rows if r["sp"] <= 5)/max(1, len([r for r in rows if r["sp"] <= 5]))) / 2
print(f"  gap {dn:+.1f}bps, of which at most {dh:+.1f}bps is the maker half-spread "
      f"-> {100*(dn-dh)/dn:.0f}% is trade selection")
