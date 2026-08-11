#!/usr/bin/env python3
"""Stage A univariate IC audit + the cost floor re-priced at MEASURED R2.

fc_costfloor.py passes the spec's Phase 1 gate, but only against an ASSUMED R2 grid of
0.1%-1.0%. That grid is a sensitivity analysis, not a measurement, and the decision
should not rest on it: the leakage-clean panel already gives a real number. For a
cross-sectional rank IC, R2 ~ IC^2, and the best single feature at h=5 measured
IC = 0.0197 -> R2 = 0.039%, which sits BELOW the most pessimistic grid point.

So this does two things:

  1. Stage A proper (spec 5A): Spearman IC per timestamp for every Tier-1 feature x
     horizon x tier, with clustered and Newey-West t-stats and effective n. This is
     ic_table.csv.
  2. Re-prices the cost floor using measured IC^2 in place of the assumed grid, which is
     the number the go/no-go should actually turn on.

Statistical handling follows spec 6 exactly:
  - IC is computed cross-sectionally per timestamp; the timestamp series is the sample.
    Pooling 177 coins x 29k timestamps and t-testing 4.3M rows manufactures t=30 for
    nothing.
  - t-stats are Newey-West corrected with lag = h bars, because at 1m bars with h=60
    consecutive labels overlap 60x.
  - effective n = n_timestamps / h_bars is reported next to every result.

  python3 analysis/fc_ic.py bars_1m.csv.gz [ic_table.csv]
"""
import math, sys
import numpy as np
import pandas as pd

BARS = sys.argv[1]
OUT = sys.argv[2] if len(sys.argv) > 2 else "ic_table.csv"
HOR = [1, 5, 15, 60, 240]
FEATS = ["ofi", "ofi5", "ofi_big", "eff_sz", "herf", "ret1", "clv", "intensity", "rv60"]
MIN_PRINTS, MIN_NTL = 10, 5000.0
COST = {"maker": 2.88, "taker": 5.73}       # measured, see fc_costfloor.py

uni = pd.read_csv("perp_universe.csv").set_index("name")["day_notional_vol"]
q1, q2 = uni.quantile([1/3, 2/3])
tiermap = {s: ("LOW" if v < q1 else ("MID" if v < q2 else "HIGH")) for s, v in uni.items()}

print("loading bars ...")
df = pd.read_csv(BARS, dtype={"coin": "category"})
df["ntl"] = df.buy_ntl + df.sell_ntl
df = df.sort_values(["coin", "bar_ms"], kind="mergesort").reset_index(drop=True)
g = df.groupby("coin", observed=True)

tot = df.ntl.replace(0, np.nan)
big_tot = (df.big_buy_ntl + df.big_sell_ntl).replace(0, np.nan)
with np.errstate(divide="ignore", invalid="ignore"):
    raw = pd.DataFrame({
        "ofi": (df.buy_ntl - df.sell_ntl) / tot,
        "ofi_big": (df.big_buy_ntl - df.big_sell_ntl) / big_tot,
        "eff_sz": df.ntl2 / tot,
        "herf": df.ntl2 / (tot ** 2),
        "ret1": np.log(df.c / df.o),
        "clv": (df.c - df.l) / (df.h - df.l).replace(0, np.nan),
        "n": df.n.astype(float),
    })

P = pd.DataFrame({"coin": df.coin, "t": df.bar_ms})
for c in raw.columns:                     # STRICTLY past: shift by one closed bar
    P[c] = raw[c].groupby(df.coin, observed=True).shift(1)
P["ofi5"] = P.groupby("coin", observed=True).ofi.transform(
    lambda s: s.rolling(5, min_periods=3).mean())
P["intensity"] = P.n / P.groupby("coin", observed=True).n.transform(
    lambda s: s.rolling(60, min_periods=20).mean())
P["rv60"] = P.groupby("coin", observed=True).ret1.transform(
    lambda s: s.rolling(60, min_periods=20).std())
P["tier"] = df.coin.map(tiermap).fillna("LOW").values
P["active"] = ((df.n >= MIN_PRINTS) & (df.ntl >= MIN_NTL)).values
O = df.o.values
print(f"  panel {len(P):,} rows, {P.active.mean()*100:.0f}% active")


def nw_t(x, lag):
    x = np.asarray(x, float); x = x[~np.isnan(x)]
    n = len(x)
    if n < 30:
        return np.nan
    e = x - x.mean()
    v = (e * e).sum() / n
    for L in range(1, min(lag, n - 1) + 1):
        v += 2 * (1 - L / (lag + 1)) * (e[L:] * e[:-L]).sum() / n
    return x.mean() / math.sqrt(v / n) if v > 0 else np.nan


def ic_series(sub, feat):
    """per-timestamp Spearman IC, fully vectorised.

    The obvious groupby().apply() version is ~29k Python calls per feature and does not
    finish. Ranks, means and the three sums are all cython-level groupby ops, so the
    whole thing is a handful of passes regardless of how many timestamps there are.
    """
    d = sub[["t", feat, "y"]].dropna()
    if len(d) < 500:
        return pd.Series(dtype=float)
    gt = d.groupby("t")
    d = d.assign(a=gt[feat].rank(), b=gt["y"].rank(), sz=gt[feat].transform("size"))
    d = d[d.sz >= 5]
    if d.empty:
        return pd.Series(dtype=float)
    gt = d.groupby("t")
    d = d.assign(da=d.a - gt.a.transform("mean"), db=d.b - gt.b.transform("mean"))
    d = d.assign(nn=d.da * d.db, aa=d.da ** 2, bb=d.db ** 2)
    agg = d.groupby("t")[["nn", "aa", "bb"]].sum()
    den = np.sqrt(agg.aa * agg.bb)
    return (agg.nn / den.replace(0, np.nan)).dropna()


rows = []
for h in HOR:
    fwd = pd.Series(O).groupby(df.coin.values, observed=True).shift(-h).values
    with np.errstate(divide="ignore", invalid="ignore"):
        P["y"] = np.log(fwd / O)
    sub_all = P[P.active]
    print(f"\n=== h = {h} bar(s) = {h}m ===")
    print(f"  {'feature':>10} {'tier':>5} {'IC':>9} {'t_clust':>8} {'t_NW':>7} "
          f"{'n_ts':>7} {'eff_n':>7} {'days+':>6} {'R2%':>7}")
    for feat in FEATS:
        for t in ("ALL", "HIGH", "MID", "LOW"):
            sub = sub_all if t == "ALL" else sub_all[sub_all.tier == t]
            s = ic_series(sub, feat)
            if len(s) < 50:
                continue
            mu = s.mean()
            tc = mu / (s.std(ddof=1) / math.sqrt(len(s)))
            tn = nw_t(s.values, h)
            eff = len(s) / h
            day = pd.to_datetime(s.index, unit="ms").date
            byday = pd.Series(s.values).groupby(day).mean()
            frac = float((np.sign(byday) == np.sign(mu)).mean())
            r2 = mu ** 2 * 100
            rows.append(dict(horizon=h, feature=feat, tier=t, ic=mu, t_clustered=tc,
                             t_nw=tn, n_timestamps=len(s), eff_n=eff,
                             day_consistency=frac, r2_pct=r2))
            if t == "ALL" or abs(tn) > 3:
                print(f"  {feat:>10} {t:>5} {mu:>+9.5f} {tc:>+8.1f} {tn:>+7.1f} "
                      f"{len(s):>7,} {eff:>7,.0f} {frac:>5.0%} {r2:>7.4f}")

IC = pd.DataFrame(rows)
IC.to_csv(OUT, index=False)
print(f"\nwrote {OUT} ({len(IC)} rows)")

# ---------------- cost floor at MEASURED R2 ----------------
print("\n" + "=" * 78)
print("=== COST FLOOR RE-PRICED AT MEASURED R2 (not the assumed grid) ===")
print("=" * 78)
sig = {}
for h in HOR:
    fwd = pd.Series(O).groupby(df.coin.values, observed=True).shift(-h).values
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.log(fwd / O) * 1e4
    for t in ("HIGH", "MID", "LOW"):
        m = P.active.values & (P.tier.values == t) & np.isfinite(r)
        sig[(h, t)] = float(np.nanstd(r[m])) if m.sum() > 100 else np.nan

print(f"\n  best single feature per horizon/tier, |t_NW| >= 3 required to count")
print(f"  {'h':>4} {'tier':>5} {'best feat':>10} {'IC':>9} {'t_NW':>7} {'R2%':>7} "
      f"{'sigma_h':>9} {'fcast bps':>10} {'maker ratio':>12} {'verdict':>9}")
best_ratio = 0.0
for h in HOR:
    for t in ("HIGH", "MID", "LOW"):
        cand = IC[(IC.horizon == h) & (IC.tier == t) & (IC.t_nw.abs() >= 3)]
        if cand.empty:
            print(f"  {h:>4} {t:>5} {'-':>10} {'-':>9} {'-':>7} {'-':>7} "
                  f"{sig[(h,t)]:>9.1f} {'-':>10} {'-':>12} {'no signal':>9}")
            continue
        b = cand.loc[cand.ic.abs().idxmax()]
        f_bps = sig[(h, t)] * abs(b.ic)          # sqrt(R2) = |IC|
        ratio = f_bps / COST["maker"]
        best_ratio = max(best_ratio, ratio)
        print(f"  {h:>4} {t:>5} {b.feature:>10} {b.ic:>+9.5f} {b.t_nw:>+7.1f} "
              f"{b.r2_pct:>7.4f} {sig[(h,t)]:>9.1f} {f_bps:>10.2f} {ratio:>12.2f} "
              f"{'PASS' if ratio >= 1.5 else 'fail':>9}")
print(f"\n  best maker ratio at measured R2: {best_ratio:.2f}  (spec 9 gate: >= 1.5)")
print(f"  -> PHASE 1 {'PASS' if best_ratio >= 1.5 else 'FAIL'} on measured R2")
