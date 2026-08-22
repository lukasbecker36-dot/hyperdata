#!/usr/bin/env python3
"""Has the market flipped from reversion to continuation? Measured, not inferred from P&L.

The fade is a bet that a volume-spike breakout reverts. If the regime has shifted so that
breakouts CONTINUE, the strategy fails exactly the way it has since 08-19 -- and no risk
control fixes that, because the edge itself would be gone rather than merely unlucky.

The test does not use the bot's P&L at all, so it cannot be contaminated by sizing, caps
or execution. For every 5x-volume-spike breakout in the universe it measures two things:

  fade_bps   the strategy's own rule -- exit on reclaim of the prior 24h range, else the
             32-bar backstop. Positive means reversion paid.
  cont_bps   the same event traded the OTHER way at fixed horizons. Positive means the
             breakout kept going, which is the direct momentum measurement the earlier
             cont_momentum.py / ts_continuation.py work was built on.

Reported per day, so a regime change shows up as a level shift rather than as a single bad
tail. The reclaim RATE is the cleanest single number: it is the fraction of breakouts that
came back inside their range at all, independent of how far.

  python3 analysis/regime_shift.py [start] [end]
"""
import json, math, sys, time, urllib.request
from datetime import datetime, timezone
import numpy as np
import pandas as pd

START = sys.argv[1] if len(sys.argv) > 1 else "2026-07-25"
END = sys.argv[2] if len(sys.argv) > 2 else "2026-08-22"
WIN, BACKSTOP, VOL_MULT, COST = 96, 32, 5.0, 3.0


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
print(f"fetching 15m candles for {len(names)} HIGH/MID perps, {START} to {END} ...")

rows = []
for i, sym in enumerate(names):
    d = post({"type": "candleSnapshot",
              "req": {"coin": sym, "interval": "15m", "startTime": t0, "endTime": t1}})
    if not d or len(d) < WIN + BACKSTOP + 5:
        continue
    g = pd.DataFrame([{"t": int(c["t"]), "h": float(c["h"]), "l": float(c["l"]),
                       "c": float(c["c"]), "v": float(c["v"])} for c in d]).sort_values("t")
    cl, hi, lo = g.c.values, g.h.values, g.l.values
    vo, tm = g.v.values, g.t.values
    med = pd.Series(vo).shift(1).rolling(WIN).median().values
    ph = pd.Series(hi).shift(1).rolling(WIN).max().values
    pl = pd.Series(lo).shift(1).rolling(WIN).min().values
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = vo / med
    brk = np.where(cl > ph, 1, np.where(cl < pl, -1, 0))
    for j in np.nonzero((vr >= VOL_MULT) & (brk != 0))[0]:
        if j + BACKSTOP >= len(cl) or tm[j] < ms(START):
            continue
        b = int(brk[j]); dd = -b; e = cl[j]
        ret, bars, why = None, BACKSTOP, "backstop"
        for k in range(1, BACKSTOP + 1):
            c = cl[j + k]
            if (dd < 0 and c < ph[j]) or (dd > 0 and c > pl[j]):
                ret, bars, why = dd * (c - e) / e, k, "reclaim"; break
        if ret is None:
            ret = dd * (cl[j + BACKSTOP] - e) / e
        row = dict(sym=sym, t=int(tm[j]), dirn=b, why=why, bars=bars,
                   fade_bps=ret * 1e4 - COST)
        for h in (4, 8, 16, 32):
            if j + h < len(cl):
                row[f"cont{h}"] = b * (cl[j + h] - e) / e * 1e4
        rows.append(row)
    if (i + 1) % 40 == 0:
        print(f"  {i+1}/{len(names)}")
    time.sleep(0.02)

ev = pd.DataFrame(rows).sort_values("t").reset_index(drop=True)
ev["day"] = pd.to_datetime(ev.t, unit="ms").dt.strftime("%m-%d")
print(f"\n{len(ev):,} breakout events over {ev.day.nunique()} days\n")

print("=== per day: did breakouts revert, or keep going? ===")
print(f"  {'day':>6} {'n':>5} {'reclaim%':>9} {'fade bps':>10} {'cont+8bar':>11} "
      f"{'cont+32bar':>12}")
g = ev.groupby("day")
for d, s in g:
    print(f"  {d:>6} {len(s):>5} {100*(s.why=='reclaim').mean():>8.0f}% "
          f"{s.fade_bps.mean():>+10.1f} {s.get('cont8', pd.Series([np.nan])).mean():>+11.1f} "
          f"{s.get('cont32', pd.Series([np.nan])).mean():>+12.1f}")

cut = "08-19"
pre = ev[ev.day < cut]
post_ = ev[ev.day >= cut]
print(f"\n=== before vs from {cut} ===")
print(f"  {'window':>16} {'n':>6} {'reclaim%':>9} {'fade bps':>10} {'t':>7} "
      f"{'cont+32':>9}")
for lab, s in (("before 08-19", pre), ("08-19 onward", post_)):
    if len(s) < 20:
        continue
    m = s.fade_bps.mean()
    tt = m / (s.fade_bps.std(ddof=1) / math.sqrt(len(s)))
    print(f"  {lab:>16} {len(s):>6,} {100*(s.why=='reclaim').mean():>8.0f}% "
          f"{m:>+10.1f} {tt:>+7.2f} {s.get('cont32', pd.Series([np.nan])).mean():>+9.1f}")
print("\n  A regime change shows as a LEVEL shift in reclaim% and in cont+32bar, not as")
print("  one bad tail. If reclaim% is unchanged, the edge is intact and the loss is size.")
