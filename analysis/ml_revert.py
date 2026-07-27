#!/usr/bin/env python3
"""Can a model (incl. a neural net) predict which fades revert? An honest test.

The tempting version of this experiment fits a model, reports cross-validated accuracy,
and looks great. It is almost always wrong here, for three specific reasons:

  1. SIGNAL-TO-NOISE. Per-trade return has a std dev of ~330bps against a ~46bps edge.
     Noise is 7x the signal, so with n~2000 nothing smaller than ~15bps is detectable.
  2. CLUSTERING. Events are correlated across coins at the same instant (one market move
     fires many signals -- three same-direction longs 38s apart was observed live) and
     within a coin over time. Random k-fold CV leaks across both and is wildly optimistic.
  3. CAPACITY TO OVERFIT. A neural net has more parameters than we have samples. It will
     find structure in noise, and the flattering number will be the training score.

So this does three things differently:
  - splits by TIME, training on the earliest 60% and testing on the latest 40%. No random
    folds, no shuffling.
  - reports TRAIN and TEST side by side, so overfitting is visible rather than hidden.
  - runs a SHUFFLED-LABEL control many times. That measures what "apparent edge from pure
    noise" looks like on this dataset. Any real model must beat that distribution, not
    merely beat zero.

The decision metric is what a trader would actually do: take the top half of the model's
predicted trades and see whether they beat trading everything, out of sample.

  python3 analysis/ml_revert.py [15m|1h]
"""
import sys, warnings
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.linear_model import RidgeCV
from sklearn.neural_network import MLPRegressor
from sklearn.preprocessing import StandardScaler

IV = sys.argv[1] if len(sys.argv) > 1 else "1h"
CFG = {"15m": ("hyperliquid_15m_allperps.csv", 96, 32, 1500),
       "1h":  ("hyperliquid_1h_history.csv", 24, 8, 400)}
CANDLES, WIN, BACKSTOP, MINBARS = CFG[IV]
VOL_MULT, RV_PCTILE, COST_BPS = 5.0, 0.60, 3.0
TRAIN_FRAC = 0.60
N_SHUFFLE = 40
SEED = 11

print(f"building events ({IV}) ...")
df = pd.read_csv(CANDLES).sort_values(["symbol", "open_time_ms"]).reset_index(drop=True)
uni = pd.read_csv("perp_universe.csv").set_index("name")["day_notional_vol"]
qs = uni.quantile([1/3, 2/3]).values
fund = pd.read_csv("hyperliquid_funding.csv").sort_values(["symbol", "time_ms"])
fmap = {s: (g["time_ms"].values, g["funding_rate"].values)
        for s, g in fund.groupby("symbol", sort=False)}

rows = []
for sym, g in df.groupby("symbol", sort=False):
    if len(g) < MINBARS or uni.get(sym, 0) < qs[0]:
        continue
    g = g.reset_index(drop=True)
    cl = g["close"].values.astype(float); hi = g["high"].values.astype(float)
    lo = g["low"].values.astype(float);  vo = g["volume"].values.astype(float)
    nt = g["num_trades"].values.astype(float); tm = g["open_time_ms"].values
    med = pd.Series(vo).shift(1).rolling(WIN).median().values
    ph = pd.Series(hi).shift(1).rolling(WIN).max().values
    pl = pd.Series(lo).shift(1).rolling(WIN).min().values
    lr = np.full(len(cl), np.nan); lr[1:] = np.log(cl[1:]/cl[:-1])
    rv = pd.Series(lr).rolling(WIN).std().values
    aps = np.divide(vo, nt, out=np.zeros_like(vo), where=nt > 0)
    ma = pd.Series(aps).shift(1).rolling(WIN).median().values
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = vo/med
    brk = np.where(cl > ph, 1, np.where(cl < pl, -1, 0))
    ft, fr = fmap.get(sym, (None, None))
    if ft is None:
        continue
    tier = 1.0 if uni.get(sym, 0) >= qs[1] else 0.0
    for i in np.nonzero((vr >= VOL_MULT) & (brk != 0) & ~np.isnan(rv) & ~np.isnan(ma))[0]:
        if i + BACKSTOP >= len(cl):
            continue
        j = np.searchsorted(ft, tm[i], side="right") - 1
        if j < 0:
            continue
        f = fr[j]
        if (1 if f > 0 else (-1 if f < 0 else 0)) != brk[i]:
            continue
        d = -int(brk[i]); entry = cl[i]; ret = None
        for k in range(1, BACKSTOP+1):
            c = cl[i+k]
            if (d < 0 and c < ph[i]) or (d > 0 and c > pl[i]):
                ret = d*(c-entry)/entry; break
        if ret is None:
            ret = d*(cl[i+BACKSTOP]-entry)/entry
        rng = (ph[i]-pl[i])/entry
        pierce = (cl[i]-ph[i])/ph[i] if brk[i] == 1 else (pl[i]-cl[i])/pl[i]
        rows.append(dict(
            t=tm[i], sym=sym, y=ret*1e4 - COST_BPS,
            vratio=vr[i], rv=rv[i], pierce=pierce*1e4,
            pierce_frac=pierce/rng if rng > 0 else 0.0, range_bps=rng*1e4,
            ats=aps[i]/ma[i] if ma[i] > 0 else 1.0, fabs=abs(f)*1e6,
            is_short=1.0 if d < 0 else 0.0, tier=tier,
            hour=pd.Timestamp(tm[i], unit="ms", tz="UTC").hour))
ev = pd.DataFrame(rows).sort_values("t").reset_index(drop=True)
ev = ev[ev["rv"] >= ev["rv"].quantile(RV_PCTILE)].reset_index(drop=True)
FEATS = ["vratio", "rv", "pierce", "pierce_frac", "range_bps", "ats", "fabs",
         "is_short", "tier", "hour"]
print(f"events: {len(ev):,}   features: {len(FEATS)}   "
      f"target std: {ev['y'].std():.0f}bps  mean: {ev['y'].mean():+.1f}bps")

cut = int(len(ev)*TRAIN_FRAC)
tr, te = ev.iloc[:cut], ev.iloc[cut:]
print(f"time split: train {len(tr):,} (to {pd.Timestamp(tr['t'].iloc[-1], unit='ms').date()}) "
      f"| test {len(te):,} (from {pd.Timestamp(te['t'].iloc[0], unit='ms').date()})")
print(f"  train mean {tr['y'].mean():+.1f}bps   test mean {te['y'].mean():+.1f}bps\n")

sc = StandardScaler().fit(tr[FEATS])
Xtr, Xte = sc.transform(tr[FEATS]), sc.transform(te[FEATS])
ytr, yte = tr["y"].values, te["y"].values


def topk_edge(pred, y, frac=0.5):
    """Mean y of the top-predicted `frac` of trades, minus the mean of everything."""
    k = max(1, int(len(y)*frac))
    idx = np.argsort(pred)[-k:]
    return y[idx].mean() - y.mean(), y[idx].mean(), k


def build(name, seed=SEED):
    if name == "ridge":
        return RidgeCV(alphas=np.logspace(-2, 4, 25))
    if name == "gbm":
        return GradientBoostingRegressor(n_estimators=150, max_depth=3,
                                         learning_rate=0.05, subsample=0.8,
                                         random_state=seed)
    if name == "nn-small":
        return MLPRegressor(hidden_layer_sizes=(8,), alpha=1.0, max_iter=2000,
                            early_stopping=True, random_state=seed)
    if name == "nn-big":
        return MLPRegressor(hidden_layer_sizes=(64, 32), alpha=1e-4, max_iter=2000,
                            random_state=seed)


print("=== models: top-half selection edge (bps vs trading everything) ===")
print(f"  {'model':>10} {'TRAIN edge':>12} {'TEST edge':>11} {'test mean':>11} {'n sel':>7}")
real = {}
for name in ("ridge", "gbm", "nn-small", "nn-big"):
    m = build(name).fit(Xtr, ytr)
    etr, _, _ = topk_edge(m.predict(Xtr), ytr)
    ete, mte, k = topk_edge(m.predict(Xte), yte)
    real[name] = ete
    print(f"  {name:>10} {etr:>+12.1f} {ete:>+11.1f} {mte:>+11.1f} {k:>7}")

print(f"\n=== the noise floor: same models, LABELS SHUFFLED ({N_SHUFFLE} runs) ===")
print("  if a real TEST edge is inside this distribution, it is indistinguishable from luck")
print(f"  {'model':>10} {'mean':>8} {'p05':>8} {'p50':>8} {'p95':>8} "
      f"{'real':>8} {'percentile':>11}")
rng = np.random.default_rng(SEED)
for name in ("ridge", "gbm", "nn-small", "nn-big"):
    outs = []
    for s in range(N_SHUFFLE):
        ysh = rng.permutation(ytr)
        m = build(name, seed=s).fit(Xtr, ysh)
        e, _, _ = topk_edge(m.predict(Xte), yte)
        outs.append(e)
    outs = np.array(outs)
    pct = (outs < real[name]).mean()*100
    print(f"  {name:>10} {outs.mean():>+8.1f} {np.percentile(outs,5):>+8.1f} "
          f"{np.percentile(outs,50):>+8.1f} {np.percentile(outs,95):>+8.1f} "
          f"{real[name]:>+8.1f} {pct:>10.0f}%")

print("\nReading it: 'percentile' is where the real model lands in the shuffled-label")
print("distribution. Above ~95% means it beat noise. Anywhere near 50% means the model")
print("learned nothing that generalises, however good its TRAIN edge looked.")
