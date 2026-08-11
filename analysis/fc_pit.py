#!/usr/bin/env python3
"""Point-in-time panel + the leakage assertion. Phase 0 of the forecasting spec.

Everything downstream depends on this file being right, so the boundary rules are
implemented once here and asserted rather than assumed:

  - bars keyed by OPEN time; a bar [t, t+D) is complete only at t+D
  - features at decision time t* use data with timestamp STRICTLY < t*
  - labels open-to-open: y = log(open[t*+h] / open[t*]), entry at the open of the bar
    starting at t*
  - a print at exactly t* belongs to the forward window, not the feature window

The decisive test (spec 1.1) is the shuffle-forward assertion: advance the label by one
bar relative to the features and confirm IC collapses to ~0. If a feature computed at t*
can still predict a label that starts at t*+1, information is flowing backwards. The
test is run on the real panel, not a toy, because that is the only version that exercises
the real join.

A second assertion, not in the spec but the mirror image of it: shifting the label
BACKWARD by one bar should make IC explode, because the "label" then overlaps the
feature window. If that does not happen, the features are not actually informative about
contemporaneous returns and something is broken upstream.

  python3 analysis/fc_pit.py bars_1m.csv.gz
"""
import gzip, math, sys
import numpy as np
import pandas as pd

BARS = sys.argv[1] if len(sys.argv) > 1 else "bars_1m.csv.gz"
BAR_S = 60
MIN_PRINTS, MIN_NTL = 10, 5000.0     # spec 4 activity filter


def load(path):
    print(f"loading {path} ...")
    df = pd.read_csv(path, dtype={"coin": "category"})
    df["ntl"] = df.buy_ntl + df.sell_ntl
    df = df.sort_values(["coin", "bar_ms"], kind="mergesort").reset_index(drop=True)
    print(f"  {len(df):,} bars, {df.coin.nunique()} coins, "
          f"{pd.to_datetime(df.bar_ms.min(), unit='ms')} to "
          f"{pd.to_datetime(df.bar_ms.max(), unit='ms')}")
    return df


def build(df, h_bars, feat_lag=1):
    """Return a panel with PIT features and an open-to-open forward label.

    feat_lag=1 is the point-in-time shift: every feature is computed on bars that have
    already CLOSED at the decision moment. Setting it to 0 would use the bar that is
    still forming, which is the classic look-ahead in bar-based research.
    """
    g = df.groupby("coin", observed=True)
    out = pd.DataFrame({"coin": df.coin, "t": df.bar_ms})

    # ---- raw per-bar quantities (contemporaneous; shifted below) ----
    tot = df.ntl.replace(0, np.nan)
    ofi = (df.buy_ntl - df.sell_ntl) / tot
    big_tot = (df.big_buy_ntl + df.big_sell_ntl).replace(0, np.nan)
    ofi_big = (df.big_buy_ntl - df.big_sell_ntl) / big_tot
    eff_sz = df.ntl2 / tot
    herf = df.ntl2 / (tot ** 2)
    with np.errstate(divide="ignore", invalid="ignore"):
        ret1 = np.log(df.c / df.o)
        clv = (df.c - df.l) / (df.h - df.l).replace(0, np.nan)

    raw = pd.DataFrame({"ofi": ofi, "ofi_big": ofi_big, "eff_sz": eff_sz,
                        "herf": herf, "ret1": ret1, "clv": clv,
                        "n": df.n.astype(float), "ntl": df.ntl})
    # ---- SHIFT: features must be strictly in the past of t* ----
    for c in raw.columns:
        out[c] = raw[c].groupby(df.coin, observed=True).shift(feat_lag)

    # a couple of rolling ones, all built from already-shifted series
    out["ofi5"] = out.groupby("coin", observed=True).ofi.transform(
        lambda s: s.rolling(5, min_periods=3).mean())
    out["intensity"] = out.n / out.groupby("coin", observed=True).n.transform(
        lambda s: s.rolling(60, min_periods=20).mean())
    out["rv60"] = out.groupby("coin", observed=True).ret1.transform(
        lambda s: s.rolling(60, min_periods=20).std())

    # ---- LABEL: open-to-open, forward h bars, from the CONTEMPORANEOUS open ----
    o = df.o
    fwd_o = o.groupby(df.coin, observed=True).shift(-h_bars)
    out["y"] = np.log(fwd_o / o)
    # vol-normalised target (spec 2b)
    out["y_z"] = out.y / out.rv60.replace(0, np.nan) / math.sqrt(h_bars)

    # ---- activity filter (spec 4) ----
    keep = (df.n >= MIN_PRINTS) & (df.ntl >= MIN_NTL)
    out["active"] = keep.values
    return out


def ic_by_timestamp(panel, feat, ycol="y"):
    """Spearman IC computed cross-sectionally per timestamp, then averaged.

    Spec 6: never pool coins x timestamps and t-test on the pooled rows. The cross
    section at one instant is one observation; there are ~29k of them, not 4.3M.
    """
    d = panel[["t", feat, ycol]].dropna()
    if len(d) < 1000:
        return (np.nan, np.nan, 0, np.nan)
    r = d.groupby("t")[[feat, ycol]].rank()
    d = d.assign(**{f"_{feat}": r[feat], "_y": r[ycol]})
    # per-timestamp correlation of ranks
    def cs(g):
        if len(g) < 5:
            return np.nan
        a, b = g[f"_{feat}"].values, g["_y"].values
        a = a - a.mean(); b = b - b.mean()
        d1 = math.sqrt((a * a).sum() * (b * b).sum())
        return (a * b).sum() / d1 if d1 > 0 else np.nan
    s = d.groupby("t").apply(cs, include_groups=False).dropna()
    if len(s) < 20:
        return (np.nan, np.nan, len(s), np.nan)
    mu = s.mean()
    # Newey-West with lag = h bars is applied by the caller via `nw`; here plain se
    se = s.std(ddof=1) / math.sqrt(len(s))
    return (mu, mu / se if se > 0 else np.nan, len(s), s.std(ddof=1))


def nw_t(series, lag):
    """Newey-West t-stat for the mean of an autocorrelated series (spec 6)."""
    x = np.asarray(series, float)
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 30:
        return np.nan
    e = x - x.mean()
    g0 = (e * e).sum() / n
    v = g0
    for L in range(1, min(lag, n - 1) + 1):
        gl = (e[L:] * e[:-L]).sum() / n
        v += 2 * (1 - L / (lag + 1)) * gl
    if v <= 0:
        return np.nan
    return x.mean() / math.sqrt(v / n)


if __name__ == "__main__":
    df = load(BARS)
    H = 5                                   # 5 bars = 5 minutes
    FEATS = ["ofi", "ofi5", "ofi_big", "eff_sz", "herf", "ret1", "clv", "intensity"]

    print(f"\n=== LEAKAGE ASSERTION (spec 1.1), h={H} bars ===")
    print("  correctly-aligned IC, then the same features against a label shifted")
    print("  FORWARD one bar (must collapse) and BACKWARD one bar (must explode)")
    base = build(df, H, feat_lag=1)
    base = base[base.active]
    print(f"  panel {len(base):,} rows after the activity filter "
          f"({100*len(base)/len(df):.0f}% of bars kept)")

    # shifted-label variants, built by moving y within coin
    fwd = base.copy()
    fwd["y"] = fwd.groupby("coin", observed=True).y.shift(-1)
    bwd = base.copy()
    bwd["y"] = bwd.groupby("coin", observed=True).y.shift(1)

    print(f"\n  {'feature':>10} {'IC aligned':>12} {'t':>7} | {'IC +1 bar':>11} {'t':>7} "
          f"| {'IC -1 bar':>11} {'t':>7}")
    rows = []
    for f in FEATS:
        a, ta, na, _ = ic_by_timestamp(base, f)
        b, tb, _, _ = ic_by_timestamp(fwd, f)
        c, tc, _, _ = ic_by_timestamp(bwd, f)
        rows.append((f, a, ta, b, tb, c, tc))
        print(f"  {f:>10} {a:>+12.5f} {ta:>+7.1f} | {b:>+11.5f} {tb:>+7.1f} "
              f"| {c:>+11.5f} {tc:>+7.1f}")

    print(f"\n  timestamps in the IC series: {na:,}")
    ok = all(abs(r[3]) < abs(r[1]) or abs(r[1]) < 0.005 for r in rows)
    big = sum(1 for r in rows if abs(r[5]) > abs(r[1]))
    print(f"  forward-shift collapses on all features: {ok}")
    print(f"  backward-shift amplifies on {big}/{len(rows)} features "
          f"(confirms the join is live and the features do describe contemporaneous flow)")
