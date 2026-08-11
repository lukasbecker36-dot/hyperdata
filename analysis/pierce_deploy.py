#!/usr/bin/env python3
"""Filter or size? The 15m deployment question for deep pierce.

pierce_transfer.py confirms the edge at 15m: deep tercile +86.5bps (t=+5.30) against a
+45.6 baseline, 3/3 months positive, halves +84.8 / +88.4, and it sorts INSIDE every rv
bucket (rv-LOW +60 vs +24, rv-MID +46 vs +6, rv-HIGH +130 vs +49) so it is not the rv
gate wearing a different hat.

But one load-bearing fact from the 1h study does NOT survive the move to 15m, and it is
the fact the "drop the shallow half" recommendation rests on:

    1h  : shallow terciles earn +1.1 and -3.8 bps  -> break-even, free to drop
    15m : shallow terciles earn +28.4 and +21.9 bps -> clearly profitable

At 1h a hard filter discards nothing. At 15m it discards trades making ~25bps each. The
live arm runs at 2.2 of 40 position slots, so capacity is not the constraint and there is
no reason to pay P&L for a smaller book. That points at sizing rather than filtering --
the same conclusion the ats question reached, for the same reason.

This prices both on the 15m gated event set, on identical entries, with every rule
normalised to mean(multiplier)=1 so "bet more" cannot masquerade as "bet better".

  python3 analysis/pierce_deploy.py
"""
import math
import numpy as np
import pandas as pd

WIN, BACKSTOP, VOL_MULT, COST_BPS = 96, 32, 5.0, 3.0
SIZE_REF, SIZE_MIN, SIZE_MAX = 2.0, 0.5, 3.0
NOT = 35.0

uni = pd.read_csv("perp_universe.csv").set_index("name")["day_notional_vol"]
qs = uni.quantile([1/3, 2/3]).values
tier = {s: ("LOW" if v < qs[0] else ("MID" if v < qs[1] else "HIGH")) for s, v in uni.items()}
fund = pd.read_csv("hyperliquid_funding.csv").sort_values(["symbol", "time_ms"])
fmap = {s: (g["time_ms"].values, g["funding_rate"].values)
        for s, g in fund.groupby("symbol", sort=False)}

print("building 15m gated events ...")
df = pd.read_csv("hyperliquid_15m_allperps.csv").sort_values(
    ["symbol", "open_time_ms"]).reset_index(drop=True)
rows = []
for sym, g in df.groupby("symbol", sort=False):
    if len(g) < 1500 or tier.get(sym, "LOW") == "LOW":
        continue
    g = g.reset_index(drop=True)
    cl = g["close"].values.astype(float); hi_ = g["high"].values.astype(float)
    lo_ = g["low"].values.astype(float); vo = g["volume"].values.astype(float)
    nt = g["num_trades"].values.astype(float); tm = g["open_time_ms"].values
    med = pd.Series(vo).shift(1).rolling(WIN).median().values
    ph = pd.Series(hi_).shift(1).rolling(WIN).max().values
    pl = pd.Series(lo_).shift(1).rolling(WIN).min().values
    lr = np.full(len(cl), np.nan); lr[1:] = np.log(cl[1:]/cl[:-1])
    rv = pd.Series(lr).rolling(WIN).std().values
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = vo/med
        ats = vo/np.maximum(nt, 1)
        ats_r = ats/pd.Series(ats).shift(1).rolling(WIN).median().values
    brk = np.where(cl > ph, 1, np.where(cl < pl, -1, 0))
    ft, fr = fmap.get(sym, (None, None))
    if ft is None:
        continue
    # causal trailing median of pierce, so the "deep" threshold uses only the past
    for i in np.nonzero((vr >= VOL_MULT) & (brk != 0) & ~np.isnan(rv))[0]:
        if i + BACKSTOP >= len(cl):
            continue
        j = np.searchsorted(ft, tm[i], side="right") - 1
        if j < 0 or (1 if fr[j] > 0 else (-1 if fr[j] < 0 else 0)) != brk[i]:
            continue
        b = int(brk[i]); d = -b; e = cl[i]
        pierce = (e - ph[i])/ph[i] if b > 0 else (pl[i] - e)/pl[i]
        ret = None
        for k in range(1, BACKSTOP+1):
            c = cl[i+k]
            if (d < 0 and c < ph[i]) or (d > 0 and c > pl[i]):
                ret = d*(c-e)/e; break
        if ret is None:
            ret = d*(cl[i+BACKSTOP]-e)/e
        rows.append(dict(t=tm[i], sym=sym, pierce=pierce, rv=rv[i],
                         ats=ats_r[i] if np.isfinite(ats_r[i]) else np.nan,
                         net=ret*1e4 - COST_BPS))
ev = pd.DataFrame(rows).sort_values("t").reset_index(drop=True)
ev = ev[ev.rv >= ev.rv.quantile(0.60)].reset_index(drop=True)
ev = ev.dropna(subset=["ats"]).reset_index(drop=True)
n = len(ev)
print(f"  {n:,} events")

# causal trailing pierce percentile: rank each event against the prior 15 days of events
t = ev.t.values; p = ev.pierce.values
pct = np.full(n, np.nan)
for i in range(n):
    lo = np.searchsorted(t, t[i] - 15*86400000, "left")
    if i - lo >= 100:
        pct[i] = (p[lo:i] < p[i]).mean()
ev["ppct"] = pct
ev = ev[ev.ppct.notna()].reset_index(drop=True)
print(f"  {len(ev):,} with a causal trailing pierce rank")


def norm(m):
    m = np.asarray(m, float)
    return m / np.nanmean(m)


def equity_stats(mult, mask=None):
    e = ev if mask is None else ev[mask]
    m = norm(mult if mask is None else np.asarray(mult)[mask.values])
    usd = NOT * m * e.net.values / 1e4
    d = pd.Series(usd, index=pd.to_datetime(e.t.values, unit="ms")).resample("D").sum()
    cum = np.cumsum(usd)
    dd = float(np.min(cum - np.maximum.accumulate(cum)))
    sh = d.mean()/d.std()*math.sqrt(365) if d.std() > 0 else np.nan
    return dict(trades=len(e), total=usd.sum(), bps=float(np.mean(e.net.values * m)),
                sharpe=sh, maxdd=dd, ret_dd=usd.sum()/abs(dd) if dd < 0 else np.nan)


ats_m = np.clip(ev.ats.values/SIZE_REF, SIZE_MIN, SIZE_MAX)
prc_m = np.clip(ev.ppct.values/0.5, SIZE_MIN, SIZE_MAX)      # tilt on the causal rank
deep = ev.ppct >= 2/3

print(f"\n=== 15m, {len(ev):,} gated events, all rules at EQUAL average capital ===")
print(f"  {'rule':<28} {'trades':>7} {'bps/trade':>10} {'total $':>9} "
      f"{'Sharpe':>7} {'maxDD':>8} {'ret/DD':>7}")
variants = [
    ("flat (baseline)", np.ones(len(ev)), None),
    ("ats only (LIVE today)", ats_m, None),
    ("pierce sizing", prc_m, None),
    ("ats x pierce sizing", ats_m*prc_m, None),
    ("deep-pierce FILTER, flat", np.ones(len(ev)), deep),
    ("deep-pierce FILTER + ats", ats_m, deep),
]
res = {}
for lab, m, msk in variants:
    r = equity_stats(m, msk)
    res[lab] = r
    print(f"  {lab:<28} {r['trades']:>7} {r['bps']:>+10.1f} {r['total']:>+9.2f} "
          f"{r['sharpe']:>+7.2f} {r['maxdd']:>+8.2f} {r['ret_dd']:>7.2f}")

print(f"\n=== what the FILTER throws away at 15m ===")
drop = ev[~deep]
m, _, k = (drop.net.mean(), None, len(drop))
print(f"  discarded: {k:,} trades averaging {m:+.1f} bps")
print(f"  at $35 that is ${NOT*k*m/1e4:+.2f} of P&L given up")
print(f"  (at 1h those same terciles earned +1.1 and -3.8 bps — genuinely free to drop)")

print(f"\n=== interaction with the rv gate now live at the 40th percentile ===")
print("  the gate was just lowered to add lower-vol signals; pierce correlates with rv,")
print("  so the filter would disproportionately remove exactly what the gate added:")
for lab, lo, hi in (("rv 40-60th (the added band)", 0.40, 0.60), ("rv >=60th (always traded)", 0.60, 1.01)):
    rvq = ev.rv.rank(pct=True)
    s = ev[(rvq >= lo) & (rvq < hi)]
    if len(s) < 30:
        continue
    kept = (s.ppct >= 2/3).mean()
    print(f"  {lab:<28} n={len(s):>5}  survives the deep filter: {kept:>4.0%}  "
          f"deep {s[s.ppct>=2/3].net.mean():+7.1f} vs shallow {s[s.ppct<2/3].net.mean():+7.1f} bps")
