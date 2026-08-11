#!/usr/bin/env python3
"""Does the deep-pierce edge transfer from 1h research to the 15m arm that actually trades?

entry_geometry.py / pierce_equity.py / combined_equity.py establish pierce depth as the first
robust NEW positive result of the conditioning search: top tercile +68.7bps vs +22 baseline,
positive in every month, stronger out-of-sample, and worth return/|DD| 4.06 -> 5.72 with half
the trades and a 27% smaller drawdown.

All of that is measured on 1h bars (wide_stop.py reads hyperliquid_1h_history.csv, 24-bar
windows, 8-bar holds). The live arm trades 15m bars with 96-bar windows and 32-bar holds. The
pierce QUANTITY is the same economic thing at either frequency -- how far the breakout closed
beyond the prior 24 hours of range -- but "same idea" is not "same edge", and the deployment
decision rests on the 15m version.

Two independent transfers, both fully out of sample relative to the 1h study:

  A. 15m, 2026-05-26 to 2026-07-17 (hyperliquid_15m_allperps.csv)
     Same period as part of the 1h sample but a different bar size, so it isolates frequency.

  B. 15m, 2026-07-21 to 2026-08-09 (tape_events_featured.csv, 4,833 events)
     A period the 1h file does not contain at all -- the candle files end 07-17. Different
     bar size AND different dates. This is the real holdout.

Reported the way every other conditioning result here has been judged: monotonicity, monthly
consistency, and whether it survives inside rv buckets rather than proxying the rv gate.

  python3 analysis/pierce_transfer.py
"""
import math
import numpy as np
import pandas as pd

WIN, BACKSTOP, VOL_MULT, COST_BPS = 96, 32, 5.0, 3.0


def st(v):
    v = np.asarray(v, float); v = v[~np.isnan(v)]
    n = len(v)
    return (v.mean(), v.mean() / (v.std(ddof=1) / math.sqrt(n)), n) if n > 1 else (np.nan,)*3


def terciles(x):
    q = np.nanquantile(x, [1/3, 2/3])
    return np.where(x <= q[0], "LOW", np.where(x <= q[1], "MID", "HIGH"))


def report(tag, ev):
    """ev needs: pierce, net_bps, mo, rv, half"""
    n = len(ev)
    b, tb, _ = st(ev.net_bps)
    print(f"\n### {tag}  —  {n:,} events, baseline {b:+.1f} bps (t={tb:+.2f})")
    ev = ev.copy()
    ev["pt"] = terciles(ev.pierce.values)
    print(f"  {'tercile':>8} {'n':>6} {'net bps':>9} {'t':>7} {'range of pierce':>22}")
    for k in ("LOW", "MID", "HIGH"):
        s = ev[ev.pt == k]
        if len(s) < 20:
            continue
        m, t, kk = st(s.net_bps)
        print(f"  {k:>8} {kk:>6} {m:>+9.1f} {t:>+7.2f} "
              f"{f'{s.pierce.min():+.4f} to {s.pierce.max():+.4f}':>22}")
    hi = ev[ev.pt == "HIGH"]
    m, t, k = st(hi.net_bps)
    lift = m - b
    print(f"  deep-pierce lift vs baseline: {lift:+.1f} bps  "
          f"({'CONFIRMS' if lift > 0 and t > 2 else 'does not confirm'} the 1h result)")

    # monthly consistency -- the test that has killed everything else here
    g = hi.groupby("mo").net_bps.agg(["mean", "size"])
    g = g[g["size"] >= 15]
    pos = int((g["mean"] > 0).sum())
    print(f"  months positive: {pos}/{len(g)}   " +
          "  ".join(f"{m}:{r['mean']:+.0f}" for m, r in g.iterrows()))

    # halves
    for lab in ("1st", "2nd"):
        s = hi[hi.half == lab]
        if len(s) > 20:
            m2, t2, k2 = st(s.net_bps)
            print(f"  {lab} half: {m2:+.1f} bps (n={k2}, t={t2:+.2f})")

    # is it just the rv gate?
    if ev.rv.notna().any():
        ev["rq"] = terciles(ev.rv.values)
        cells = []
        for rq in ("LOW", "MID", "HIGH"):
            s = ev[(ev.rq == rq)]
            if len(s) < 60:
                continue
            a = st(s[s.pt == "HIGH"].net_bps)[0]
            c = st(s[s.pt != "HIGH"].net_bps)[0]
            cells.append(f"rv-{rq}: deep {a:+.0f} vs rest {c:+.0f}")
        print("  within rv buckets — " + " | ".join(cells))
        print(f"  corr(pierce, rv) = {np.corrcoef(ev.pierce.fillna(0), ev.rv.fillna(0))[0,1]:+.3f}")


# ---------------------------------------------------------------- A: 15m, May-Jul
print("=" * 78)
print("TRANSFER A — 15m bars, 2026-05-26 to 2026-07-17 (isolates BAR SIZE)")
print("=" * 78)
df = pd.read_csv("hyperliquid_15m_allperps.csv").sort_values(
    ["symbol", "open_time_ms"]).reset_index(drop=True)
uni = pd.read_csv("perp_universe.csv").set_index("name")["day_notional_vol"]
qs = uni.quantile([1/3, 2/3]).values
tier = {s: ("LOW" if v < qs[0] else ("MID" if v < qs[1] else "HIGH")) for s, v in uni.items()}
fund = pd.read_csv("hyperliquid_funding.csv").sort_values(["symbol", "time_ms"])
fmap = {s: (g["time_ms"].values, g["funding_rate"].values)
        for s, g in fund.groupby("symbol", sort=False)}

rows = []
for sym, g in df.groupby("symbol", sort=False):
    if len(g) < 1500 or tier.get(sym, "LOW") == "LOW":
        continue
    g = g.reset_index(drop=True)
    cl = g["close"].values.astype(float); hi_ = g["high"].values.astype(float)
    lo_ = g["low"].values.astype(float); vo = g["volume"].values.astype(float)
    tm = g["open_time_ms"].values
    med = pd.Series(vo).shift(1).rolling(WIN).median().values
    ph = pd.Series(hi_).shift(1).rolling(WIN).max().values
    pl = pd.Series(lo_).shift(1).rolling(WIN).min().values
    lr = np.full(len(cl), np.nan); lr[1:] = np.log(cl[1:]/cl[:-1])
    rv = pd.Series(lr).rolling(WIN).std().values
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = vo/med
    brk = np.where(cl > ph, 1, np.where(cl < pl, -1, 0))
    ft, fr = fmap.get(sym, (None, None))
    if ft is None:
        continue
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
        rows.append(dict(t=tm[i], pierce=pierce, rv=rv[i],
                         net_bps=ret*1e4 - COST_BPS))
A = pd.DataFrame(rows).sort_values("t").reset_index(drop=True)
A = A[A.rv >= A.rv.quantile(0.60)].reset_index(drop=True)
A["mo"] = pd.to_datetime(A.t, unit="ms").dt.strftime("%Y-%m")
A["half"] = np.where(A.t <= A.t.median(), "1st", "2nd")
report("15m, May–Jul, rv-gated HIGH+MID", A)

# ---------------------------------------------------------------- B: tape window
print("\n" + "=" * 78)
print("TRANSFER B — 15m bars, 2026-07-21 to 2026-08-09 (BAR SIZE **and** DATES; true holdout)")
print("=" * 78)
B = pd.read_csv("tape_events_featured.csv")
B = B[B.signalled == 1].copy()
B["pierce"] = np.where(B.dirn > 0, (B.entry - B.prior_h)/B.prior_h,
                       (B.prior_l - B.entry)/B.prior_l)
B["net_bps"] = B.fade_bps
B["mo"] = pd.to_datetime(B.t, unit="ms").dt.strftime("%Y-%m-%d").str.slice(0, 10)
B["mo"] = pd.to_datetime(B.t, unit="ms").dt.strftime("%m-%d")
B["half"] = np.where(B.t <= B.t.median(), "1st", "2nd")
report("15m, tape window, gated", B)

# also the ungated version, which is 6x bigger
B2 = pd.read_csv("tape_events_featured.csv").copy()
B2["pierce"] = np.where(B2.dirn > 0, (B2.entry - B2.prior_h)/B2.prior_h,
                        (B2.prior_l - B2.entry)/B2.prior_l)
B2["net_bps"] = B2.fade_bps
B2["mo"] = pd.to_datetime(B2.t, unit="ms").dt.strftime("%m-%d")
B2["half"] = np.where(B2.t <= B2.t.median(), "1st", "2nd")
report("15m, tape window, ALL spikes (6x sample)", B2)
