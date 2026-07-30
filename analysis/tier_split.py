#!/usr/bin/env python3
"""What would MID-only (the '15m-mid' arm) have earned on the fills we actually got?

Phase 2 of the original study found MID-liquidity names carried the edge and MID-only beat
HIGH+MID out of sample. The live arm trades HIGH+MID, so the MID-only variant is a subset
of the fills already taken -- no counterfactual fills needed, which makes this a clean
re-slice rather than a simulation.

Combined with flat sizing (analysis/sizing_power.py: the ats multiplier has zero
correlation with trade quality, so flat is the better default), the question is what
'MID-only + flat' would have returned on the same trades.

Tier caveat, stated up front: the bot assigns tiers from 24h notional volume tertiles
recomputed daily, and per-coin historical volume is not logged per trade. Tiers here are
computed from CURRENT volumes, so a coin that has since crossed a tertile boundary is
mislabelled. Over a few days most names are stable, but this is an approximation.

Concentration is reported for every cell, because on this dataset a headline has repeatedly
turned out to be a handful of trades.

  python3 analysis/tier_split.py [trades.csv:base ...]
"""
import csv, json, math, sys, time, urllib.request

PATHS = sys.argv[1:] or ["live_15m_ats/trades_15m.csv:25"]


def post(body, tries=6):
    for k in range(tries):
        try:
            req = urllib.request.Request("https://api.hyperliquid.xyz/info",
                                        data=json.dumps(body).encode(),
                                        headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            time.sleep(min(15, 2 ** k))
    return None


m = post({"type": "metaAndAssetCtxs"})
uni, ctxs = m[0]["universe"], m[1]
vols = {}
for u, c in zip(uni, ctxs):
    if c.get("midPx") is not None:
        vols[u["name"]] = float(c["dayNtlVlm"])
sv = sorted(vols.values())
q1, q2 = sv[len(sv)//3], sv[2*len(sv)//3]
tier = lambda s: ("LOW" if vols.get(s, 0) < q1
                  else ("MID" if vols.get(s, 0) < q2 else "HIGH"))
print(f"tier bounds from current volumes: ${q1:,.0f} / ${q2:,.0f}  ({len(vols)} perps)")

rows = []
for spec in PATHS:
    p, _, b = spec.partition(":")
    base = float(b) if b else 25.0
    n0 = len(rows)
    for r in csv.DictReader(open(p)):
        try:
            net = float(r["net_bps"]); pnl = float(r["pnl_usd"])
            if abs(net) < 1e-9:
                continue
            rows.append(dict(sym=r["symbol"], net=net, pnl=pnl,
                             mult=(pnl / (net / 1e4)) / base,
                             tier=tier(r["symbol"]), base=base))
        except Exception:
            pass
    print(f"  loaded {len(rows)-n0:>4} trades from {p} (base ${base:.0f})")
if not rows:
    print("no trades"); sys.exit(0)


def st(v):
    n = len(v)
    if n < 2: return (float("nan"), float("nan"), n)
    mu = sum(v)/n
    sd = (sum((x-mu)**2 for x in v)/(n-1))**0.5
    return (mu, (mu/(sd/math.sqrt(n)) if sd > 0 else float("nan")), n)


def cell(sel, sizing, label):
    seg = [e for e in rows if sel(e)]
    if len(seg) < 5:
        print(f"  {label:>26} {len(seg):>4}   too few"); return
    # flat = 1 unit per trade; ats = the multiplier actually used
    w = (lambda e: 1.0) if sizing == "flat" else (lambda e: e["mult"])
    # dollars at a common $25 base so arms with different bases are comparable
    d = [w(e) * 25.0 * e["net"] / 1e4 for e in seg]
    bps = [e["net"] for e in seg]
    mb, tb, n = st(bps)
    tot = sum(d)
    s = sorted(d)
    top3 = sum(s[-3:])
    print(f"  {label:>26} {n:>4} {mb:>+9.1f} {tb:>+6.1f} {tot:>+9.2f} "
          f"{tot/sum(w(e)*25.0 for e in seg)*1e4:>+9.1f} {top3/tot*100 if tot else float('nan'):>8.0f}%")


print(f"\n=== by tier and sizing  (dollars normalised to a $25 base) ===")
print(f"  {'variant':>26} {'n':>4} {'mean bps':>9} {'t':>6} {'total $':>9} "
      f"{'bps/unit':>9} {'top3 %':>9}")
for sizing in ("flat", "ats"):
    for tname, sel in (("ALL (HIGH+MID)", lambda e: True),
                       ("MID only", lambda e: e["tier"] == "MID"),
                       ("HIGH only", lambda e: e["tier"] == "HIGH")):
        cell(sel, sizing, f"{tname} + {sizing}")
    print()

print("=== tier makeup of the fills ===")
from collections import Counter
c = Counter(e["tier"] for e in rows)
for k in ("HIGH", "MID", "LOW"):
    if c.get(k):
        print(f"  {k:>5}: {c[k]:>4} trades")
print()
print("'top3 %' is the share of that cell's total P&L coming from its three best trades.")
print("Anything near or above 100% means the cell is a few trades wearing a mean.")
