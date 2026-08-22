#!/usr/bin/env python3
"""Is the CONTINUATION side tradeable in the post-08-19 regime? Candles first, then tape.

regime_shift.py established the flip: reclaim rate 76%->60%, fade -3.3 -> -104.4bps, and
the same breakouts traded the other way going from -4.8 to +224.0bps over 32 bars. +224bps
against a 2.88bps maker round trip is not a cost-floor problem, so the question is whether
it is real and whether it can be captured, not whether it clears fees.

Three things have to hold before any of this is worth acting on, and they are tested in
order because each can kill it:

  1. Is the continuation there at a TRADEABLE horizon, net of costs, or only at 8 hours
     where a single reversal erases it?
  2. Is it stable DAY BY DAY inside the new regime, or is it one or two sessions? Four
     days is four observations and that is the binding weakness of the whole idea.
  3. Does anything SELECT which breakouts continue -- pierce, ats, rv from candles, or
     flow from the tape? Without that it is a coin flip on regime persistence.

The honest prior is bad: this is a strategy fitted to the three days that just hurt us,
proposed at the moment of maximum pain, which is the classic way to turn a drawdown into a
larger one. It gets tested anyway because +224bps is too large to dismiss, but the bar for
acting is a lot higher than the bar for measuring.

  python3 analysis/momentum_regime.py [start] [end] [cut]
"""
import json, math, sys, time, urllib.request
from datetime import datetime, timezone
import numpy as np
import pandas as pd

START = sys.argv[1] if len(sys.argv) > 1 else "2026-07-25"
END = sys.argv[2] if len(sys.argv) > 2 else "2026-08-23"
CUT = sys.argv[3] if len(sys.argv) > 3 else "08-19"
WIN, VOL_MULT, MAXH = 96, 5.0, 32
COST = 5.73          # taker round trip, measured. Momentum entries cannot rest: the
                     # move is already going, so a passive order only fills if it reverses.


def post(b, tries=5):
    for k in range(tries):
        try:
            r = urllib.request.Request("https://api.hyperliquid.xyz/info",
                                       data=json.dumps(b).encode(),
                                       headers={"Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(r, timeout=30))
        except Exception:
            time.sleep(min(15, 2 ** k))
    return None


def ms(d):
    return int(datetime.strptime(d, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


t0, t1 = ms(START) - WIN * 900000, ms(END) + 900000
meta = post({"type": "metaAndAssetCtxs"})
uni = {u["name"]: c for u, c in zip(meta[0]["universe"], meta[1])
       if c.get("midPx") is not None}
dv = {s: float(uni[s].get("dayNtlVlm", 0) or 0) for s in uni}
vals = sorted(v for v in dv.values() if v > 0)
b1, b2 = vals[len(vals)//3], vals[2*len(vals)//3]
tier = {s: ("LOW" if dv[s] < b1 else ("MID" if dv[s] < b2 else "HIGH")) for s in dv}
names = [s for s in sorted(uni) if tier[s] != "LOW"]
print(f"fetching {len(names)} HIGH/MID perps, {START} to {END} ...")

HOR = [1, 2, 4, 8, 16, 32]
rows = []
for i, sym in enumerate(names):
    d = post({"type": "candleSnapshot",
              "req": {"coin": sym, "interval": "15m", "startTime": t0, "endTime": t1}})
    if not d or len(d) < WIN + MAXH + 5:
        continue
    g = pd.DataFrame([{"t": int(c["t"]), "o": float(c["o"]), "h": float(c["h"]),
                       "l": float(c["l"]), "c": float(c["c"]), "v": float(c["v"]),
                       "n": int(c["n"])} for c in d]).sort_values("t")
    cl, hi, lo, op = g.c.values, g.h.values, g.l.values, g.o.values
    vo, nt, tm = g.v.values, g.n.values.astype(float), g.t.values
    med = pd.Series(vo).shift(1).rolling(WIN).median().values
    ph = pd.Series(hi).shift(1).rolling(WIN).max().values
    pl = pd.Series(lo).shift(1).rolling(WIN).min().values
    lr = np.full(len(cl), np.nan); lr[1:] = np.log(cl[1:]/cl[:-1])
    rv = pd.Series(lr).rolling(WIN).std().values
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = vo/med
        ats = vo/np.maximum(nt, 1)
        ar = ats/pd.Series(ats).shift(1).rolling(WIN).median().values
    brk = np.where(cl > ph, 1, np.where(cl < pl, -1, 0))
    for j in np.nonzero((vr >= VOL_MULT) & (brk != 0))[0]:
        if j + MAXH >= len(cl) or tm[j] < ms(START):
            continue
        b = int(brk[j]); e = cl[j]
        pierce = (e - ph[j])/ph[j] if b > 0 else (pl[j] - e)/pl[j]
        row = dict(sym=sym, t=int(tm[j]), dirn=b, rv=float(rv[j]), vratio=float(vr[j]),
                   ats=float(ar[j]) if np.isfinite(ar[j]) else np.nan,
                   pierce=float(pierce), tier=tier[sym])
        # continuation: enter WITH the break at the next bar's OPEN (tradeable), exit at
        # the close h bars later. Entering at this bar's close would be same-bar lookahead.
        if j + 1 < len(cl):
            entry = op[j+1]
            for h in HOR:
                if j + 1 + h < len(cl):
                    row[f"m{h}"] = b*(cl[j+1+h]-entry)/entry*1e4 - COST
        rows.append(row)
    if (i+1) % 40 == 0:
        print(f"  {i+1}/{len(names)}")
    time.sleep(0.02)

ev = pd.DataFrame(rows).sort_values("t").reset_index(drop=True)
ev["day"] = pd.to_datetime(ev.t, unit="ms").dt.strftime("%m-%d")
pre, post_ = ev[ev.day < CUT], ev[ev.day >= CUT]
print(f"\n{len(ev):,} events | before {CUT}: {len(pre):,} | from {CUT}: {len(post_):,}\n")


def st(v):
    v = np.asarray(v, float); v = v[~np.isnan(v)]
    n = len(v)
    return (v.mean(), v.mean()/(v.std(ddof=1)/math.sqrt(n)), n) if n > 1 else (np.nan,)*3


print(f"=== 1. MOMENTUM BY HORIZON, net of {COST:.2f}bps taker round trip ===")
print(f"  {'horizon':>9} " + " ".join(f"{('before'):>16}" for _ in [0]) +
      " ".join(f"{('from '+CUT):>18}" for _ in [0]))
for h in HOR:
    a, ta, na = st(pre.get(f"m{h}", pd.Series(dtype=float)))
    b_, tb, nb = st(post_.get(f"m{h}", pd.Series(dtype=float)))
    print(f"  {h:>4} bars  {a:>+9.1f} (t={ta:>+5.2f})  {b_:>+9.1f} (t={tb:>+5.2f})  n={nb:,}")

print(f"\n=== 2. DAY BY DAY inside the new regime (the binding weakness) ===")
print(f"  {'day':>6} {'n':>5} " + " ".join(f"{'+'+str(h)+'b':>9}" for h in (2, 4, 8, 32)))
for d, s in post_.groupby("day"):
    print(f"  {d:>6} {len(s):>5} " +
          " ".join(f"{s.get(f'm{h}', pd.Series([np.nan])).mean():>+9.1f}" for h in (2, 4, 8, 32)))
print(f"  {'PRIOR':>6} {len(pre):>5} " +
      " ".join(f"{pre.get(f'm{h}', pd.Series([np.nan])).mean():>+9.1f}" for h in (2, 4, 8, 32)))

print(f"\n=== 3. DOES ANYTHING SELECT the continuers? (post-{CUT}, best horizon) ===")
best_h = max(HOR, key=lambda h: st(post_.get(f"m{h}", pd.Series(dtype=float)))[0]
             if f"m{h}" in post_ else -1e9)
print(f"  using +{best_h} bars")
for feat in ("pierce", "ats", "rv", "vratio"):
    s = post_[post_[feat].notna() & post_[f"m{best_h}"].notna()].copy()
    if len(s) < 100:
        continue
    s["q"] = pd.qcut(s[feat], 3, labels=["LOW", "MID", "HIGH"], duplicates="drop")
    m = s.groupby("q", observed=True)[f"m{best_h}"].agg(["mean", "size"])
    r = np.corrcoef(s[feat], s[f"m{best_h}"])[0, 1]
    print(f"  {feat:>8}  " + "  ".join(f"{k}:{v['mean']:>+7.0f}(n={int(v['size'])})"
                                       for k, v in m.iterrows()) +
          f"   corr {r:+.3f}")
ev.to_csv("momentum_events.csv", index=False)
print(f"\nwrote momentum_events.csv ({len(ev):,} rows) for the tape/vpin join")
