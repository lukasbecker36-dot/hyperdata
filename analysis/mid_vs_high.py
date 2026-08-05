#!/usr/bin/env python3
"""MID vs HIGH on real trades, using CONTEMPORANEOUS tiers.

analysis/tier_history.py established that rolling 24h notional rebuilt from candleSnapshot
reproduces the bot's own logged tier bounds to a 1% median error, so historical tiers are
recoverable. This applies them to the actual trade logs, which the current-volume version
could not do correctly (~10% of names are mislabelled within two weeks, and that is what
made the earlier tier analyses contradict each other).

Answers: what would MID-only ('15m-mid') have earned on the fills we actually took, and
does the Phase 2 claim that MID carries the edge hold up on live and paper trades?

Concentration is reported for every cell, because on this dataset headline numbers have
repeatedly turned out to be three trades.

  python3 analysis/mid_vs_high.py [trades.csv:base ...]
"""
import csv, json, math, sys, time, urllib.request
from datetime import datetime, timezone

PATHS = sys.argv[1:] or ["live_15m_ats/trades_15m.csv:25"]
WIN_MS = 86400000
COST = 0.0                      # net_bps already includes fees


def post(body, tries=6):
    for k in range(tries):
        try:
            req = urllib.request.Request("https://api.hyperliquid.xyz/info",
                                        data=json.dumps(body).encode(),
                                        headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            time.sleep(min(20, 2 ** k))
    return None


def pms(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


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
            rows.append(dict(sym=r["symbol"], net=net, pnl=pnl, base=base,
                             mult=(pnl / (net / 1e4)) / base, t=pms(r["entry_time"]),
                             logged_tier=(r.get("tier") or "").strip()))
        except Exception:
            pass
    print(f"loaded {len(rows)-n0:>4} from {p} (base ${base:.0f})")
if not rows:
    print("no trades"); sys.exit(0)

lo = min(r["t"] for r in rows) - 2 * WIN_MS
hi = max(r["t"] for r in rows) + WIN_MS
m = post({"type": "metaAndAssetCtxs"})
names = [u["name"] for u, c in zip(m[0]["universe"], m[1]) if c.get("midPx") is not None]
print(f"fetching candles for {len(names)} perps ...")
vol = {}
for i, s in enumerate(names):
    d = post({"type": "candleSnapshot",
              "req": {"coin": s, "interval": "15m", "startTime": lo, "endTime": hi}})
    if d:
        try:
            vol[s] = sorted((int(c["T"]), float(c["v"]) * float(c["c"])) for c in d)
        except Exception:
            pass
    time.sleep(0.03)
print(f"got {len(vol)} coins\n")


def day_ntl(sym, at):
    v = vol.get(sym)
    if not v:
        return None
    tot, n = 0.0, 0
    for t, x in v:
        if at - WIN_MS <= t < at:
            tot += x; n += 1
    return tot if n >= 48 else None


_cache = {}


def tier_at(sym, at):
    """Tier as the bot would have assigned it, tertiles recomputed at that hour."""
    key = at // 3600000
    if key not in _cache:
        vals = sorted(x for x in (day_ntl(s, at) for s in vol) if x)
        _cache[key] = (vals[len(vals)//3], vals[2*len(vals)//3]) if len(vals) >= 30 else None
    b = _cache[key]
    if not b:
        return None
    v = day_ntl(sym, at)
    if v is None:
        return None
    return "LOW" if v < b[0] else ("MID" if v < b[1] else "HIGH")


for r in rows:
    r["tier"] = tier_at(r["sym"], r["t"])

# agreement with the bot's own label, where we have it
both = [r for r in rows if r["logged_tier"] and r["tier"]]
if both:
    agree = sum(1 for r in both if r["logged_tier"] == r["tier"])
    print(f"cross-check vs the bot's own logged tier: {agree}/{len(both)} agree\n")


def st(v):
    n = len(v)
    if n < 2: return (float("nan"), float("nan"), n)
    mu = sum(v)/n
    sd = (sum((x-mu)**2 for x in v)/(n-1))**0.5
    return (mu, (mu/(sd/math.sqrt(n)) if sd > 0 else float("nan")), n)


print("=== by CONTEMPORANEOUS tier ===")
print(f"  {'tier':>6} {'n':>5} {'mean bps':>10} {'t':>7} {'win%':>6} {'flat $/25':>11} "
      f"{'top3 share':>11}")
for tn in ("HIGH", "MID", "LOW", None):
    seg = [r for r in rows if r["tier"] == tn]
    if len(seg) < 5:
        continue
    b = [r["net"] for r in seg]
    d = sorted(25.0 * r["net"] / 1e4 for r in seg)
    mu, t, n = st(b)
    tot = sum(d)
    print(f"  {str(tn):>6} {n:>5} {mu:>+10.1f} {t:>+7.1f} "
          f"{100*sum(1 for x in b if x>0)/n:>5.0f}% {tot:>+11.2f} "
          f"{(sum(d[-3:])/tot*100 if tot else float('nan')):>10.0f}%")
un = [r for r in rows if r["tier"] is None]
if un:
    print(f"  (unclassifiable: {len(un)})")

print("\n=== the question: MID-only vs trading everything, flat sizing ===")
allr = [r for r in rows if r["tier"] in ("HIGH", "MID")]
mid = [r for r in allr if r["tier"] == "MID"]
for lab, seg in (("ALL (HIGH+MID)", allr), ("MID only", mid)):
    if len(seg) < 5:
        continue
    b = [r["net"] for r in seg]
    mu, t, n = st(b)
    print(f"  {lab:>16} n={n:<4} {mu:>+7.1f} bps  t={t:>+5.1f}  "
          f"total ${25.0*sum(b)/1e4:>+7.2f} at a $25 flat base")
