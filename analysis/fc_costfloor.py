#!/usr/bin/env python3
"""Phase 1: the cost floor. Run before any feature engineering (spec 7).

    forecastable_component_bps = sigma_h_bps * sqrt(R2)
    round_trip_cost_bps        = fees + slippage + adverse_selection
    ratio                      = forecastable / cost

Pass condition (spec 9, Phase 1): maker ratio >= 1.5 at at least one horizon/tier.
If maker ratio < 1.5 everywhere, stop and report that.

Every cost input is measured, not assumed:

  fees        realised fee_bps from the live log, split by crossed/rested. These are
              already ROUND TRIP (entry + exit fee over notional), so they are not
              doubled again -- doubling would be the easy mistake here.
  slippage    entry_px against the mid at entry, (entry_bid+entry_ask)/2, signed by
              direction so positive means we paid away from mid.
  adverse     forward return after FILLS minus after MISSES, from the tape bars. The
  selection   live log and the tape DO overlap (2026-07-26 onward), so this one join is
              legitimate -- unlike joining flow to the candle files, which spec 11
              forbids because those ranges are disjoint.

sigma_h is empirical per tier per horizon, from tape-rebuilt bars only.

  python3 analysis/fc_costfloor.py bars_1m.csv.gz trades.csv misses.csv [out.csv]
"""
import csv, math, sys
from datetime import datetime, timezone
import numpy as np
import pandas as pd

BARS = sys.argv[1]
TRADES = sys.argv[2]
MISSES = sys.argv[3]
OUT = sys.argv[4] if len(sys.argv) > 4 else "cost_floor.csv"
HORIZONS = [(1, "1m"), (5, "5m"), (15, "15m"), (60, "60m"), (240, "4h")]
R2_GRID = [0.001, 0.003, 0.005, 0.010]
MIN_PRINTS, MIN_NTL = 10, 5000.0


def st(v):
    v = np.asarray(v, float); v = v[~np.isnan(v)]
    n = len(v)
    return (v.mean(), v.mean() / (v.std(ddof=1) / math.sqrt(n)), n) if n > 1 else (np.nan,)*3


# ---------------- tiers ----------------
uni = pd.read_csv("perp_universe.csv").set_index("name")["day_notional_vol"]
q1, q2 = uni.quantile([1/3, 2/3])
tier = {s: ("LOW" if v < q1 else ("MID" if v < q2 else "HIGH")) for s, v in uni.items()}

# ---------------- sigma_h per tier per horizon ----------------
print("loading bars ...")
df = pd.read_csv(BARS, dtype={"coin": "category"})
df["ntl"] = df.buy_ntl + df.sell_ntl
df = df.sort_values(["coin", "bar_ms"], kind="mergesort").reset_index(drop=True)
df["tier"] = df.coin.map(tier).fillna("LOW")
active = (df.n >= MIN_PRINTS) & (df.ntl >= MIN_NTL)
print(f"  {len(df):,} bars; {active.mean()*100:.0f}% pass the activity filter")

print("\n=== sigma_h (bps), open-to-open, per tier ===")
print(f"  {'horizon':>8} " + " ".join(f"{t:>12}" for t in ("HIGH", "MID", "LOW")))
sig = {}
g = df.groupby("coin", observed=True)
for hb, hl in HORIZONS:
    fwd = g.o.shift(-hb)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.log(fwd / df.o) * 1e4
    cells = []
    for t in ("HIGH", "MID", "LOW"):
        m = active & (df.tier == t) & np.isfinite(r)
        s = float(np.nanstd(r[m])) if m.sum() > 100 else np.nan
        sig[(hl, t)] = s
        cells.append(f"{s:>12.1f}")
    print(f"  {hl:>8} " + " ".join(cells))

# ---------------- costs from the live logs ----------------
print("\n=== measured costs ===")
tr = [r for r in csv.DictReader(open(TRADES)) if abs(float(r["net_bps"] or 0)) > 1e-9]
fee_cross = st([float(r["fee_bps"]) for r in tr
                if r.get("crossed") == "1" and r.get("fee_bps")])
fee_rest = st([float(r["fee_bps"]) for r in tr
               if r.get("crossed") == "0" and r.get("fee_bps")])
print(f"  fees, round trip:  taker-entry {fee_cross[0]:.2f} bps (n={fee_cross[2]})   "
      f"maker-entry {fee_rest[0]:.2f} bps (n={fee_rest[2]})")

slip = []
for r in tr:
    try:
        b, a = float(r["entry_bid"]), float(r["entry_ask"])
        px = float(r["entry_px"])
        mid = 0.5 * (b + a)
        if mid <= 0:
            continue
        d = 1.0 if r["side"] == "LONG" else -1.0
        slip.append(d * (px - mid) / mid * 1e4)     # >0 = paid away from mid
    except Exception:
        pass
sl_all = st(slip)
sl_x = st([s for s, r in zip(slip, [r for r in tr if r.get("entry_bid")])
           if True][:0]) if False else None
print(f"  entry slippage vs mid: {sl_all[0]:+.2f} bps mean (n={sl_all[2]}), "
      f"median {np.median(slip):+.2f}")

# ---------------- adverse selection: fills vs misses ----------------
print("\n  adverse selection (spec 8): forward return after FILLS vs after MISSES")
bars = df[["coin", "bar_ms", "o"]].copy()
bars["key"] = bars.coin.astype(str)
idx = {}
for c, gg in bars.groupby("key", sort=False):
    idx[c] = (gg.bar_ms.values, gg.o.values)


def fwd_bps(sym, t_ms, dirn, mins):
    p = idx.get(sym)
    if p is None:
        return np.nan
    tt, oo = p
    i = np.searchsorted(tt, t_ms, "left")
    j = np.searchsorted(tt, t_ms + mins * 60000, "left")
    if i >= len(tt) or j >= len(tt) or i == j:
        return np.nan
    return dirn * math.log(oo[j] / oo[i]) * 1e4


def pms(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


fills, missed = [], []
for r in tr:
    try:
        d = 1.0 if r["side"] == "LONG" else -1.0
        fills.append(fwd_bps(r["symbol"], pms(r["entry_time"]), d, 15))
    except Exception:
        pass
for r in csv.DictReader(open(MISSES)):
    try:
        d = 1.0 if r["side"] == "LONG" else -1.0
        missed.append(fwd_bps(r["symbol"], pms(r["time"]), d, 15))
    except Exception:
        pass
mf, tf, nf = st(fills)
mm, tm, nm = st(missed)
adverse = mm - mf                      # >0 means the ones we MISSED did better
print(f"    after fills  {mf:+7.1f} bps (n={nf})")
print(f"    after misses {mm:+7.1f} bps (n={nm})")
print(f"    adverse selection = missed - filled = {adverse:+.1f} bps"
      f"  ({'passive fills ARE being picked off' if adverse > 0 else 'no evidence of picking off'})")
adverse_cost = max(0.0, adverse)

# ---------------- the ratio table ----------------
rows = []
print(f"\n=== COST FLOOR: forecastable / round-trip cost ===")
print(f"  round-trip cost, maker entry = {fee_rest[0]:.2f} fees "
      f"+ {max(0,sl_all[0]):.2f} slip + {adverse_cost:.1f} adverse "
      f"= {fee_rest[0] + max(0,sl_all[0]) + adverse_cost:.2f} bps")
print(f"  round-trip cost, taker entry = {fee_cross[0]:.2f} fees "
      f"+ {max(0,sl_all[0]):.2f} slip + {adverse_cost:.1f} adverse "
      f"= {fee_cross[0] + max(0,sl_all[0]) + adverse_cost:.2f} bps")
cost_maker = fee_rest[0] + max(0, sl_all[0]) + adverse_cost
cost_taker = fee_cross[0] + max(0, sl_all[0]) + adverse_cost

for exec_lab, cost in (("maker", cost_maker), ("taker", cost_taker)):
    print(f"\n  --- {exec_lab.upper()} (cost {cost:.2f} bps) ---")
    print(f"  {'horizon':>8} {'tier':>6} {'sigma_h':>9} " +
          " ".join(f"{'R2=' + format(r*100, '.1f') + '%':>10}" for r in R2_GRID))
    for hb, hl in HORIZONS:
        for t in ("HIGH", "MID", "LOW"):
            s = sig[(hl, t)]
            if not np.isfinite(s):
                continue
            cells = []
            for r2 in R2_GRID:
                ratio = s * math.sqrt(r2) / cost
                cells.append(f"{ratio:>10.2f}")
                rows.append(dict(horizon=hl, tier=t, execution=exec_lab,
                                 sigma_h_bps=round(s, 2), r2=r2,
                                 forecastable_bps=round(s*math.sqrt(r2), 2),
                                 cost_bps=round(cost, 2), ratio=round(ratio, 3)))
            print(f"  {hl:>8} {t:>6} {s:>9.1f} " + " ".join(cells))

pd.DataFrame(rows).to_csv(OUT, index=False)
mk = [r for r in rows if r["execution"] == "maker"]
best = max(mk, key=lambda r: r["ratio"])
print(f"\nwrote {OUT} ({len(rows)} rows)")
print(f"\n=== PHASE 1 VERDICT (spec 9: pass if maker ratio >= 1.5 somewhere) ===")
print(f"  best maker ratio: {best['ratio']:.2f}  at {best['horizon']}/{best['tier']} "
      f"assuming R2={best['r2']*100:.1f}%")
n_pass = sum(1 for r in mk if r["ratio"] >= 1.5)
print(f"  cells clearing 1.5: {n_pass} of {len(mk)}")
print(f"  -> {'PASS' if best['ratio'] >= 1.5 else 'FAIL — stop and report'}")
