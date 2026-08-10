#!/usr/bin/env python3
"""Build the full event set over the tape window, with fade outcomes, for flow testing.

vpin_edge.py tested three flow features on 273 FILLED trades and could not resolve
anything under ~124bps. tape_inventory.py showed why: the filled set is 6% of what
exists. The entry gates are there to pick TRADES; they are the wrong filter for asking
whether a FEATURE carries information, and dropping them multiplies the sample 18x.

This writes every 5x-volume-spike breakout in the tape window with:
  - the outcome the fade WOULD have had (reclaim or 32-bar backstop, as the bot trades it)
  - which gates it passed, so the study can widen or narrow the population deliberately
  - the forward return at fixed horizons, so momentum can be tested on the same events

Outcomes come from candles, exactly as every backtest here simulates the fade. They are
frictionless in the sense that no fill is assumed -- which is correct for a feature study,
where the question is whether the feature sorts outcomes, not what the bot would net.

  python3 analysis/tape_events.py [out.csv]
"""
import json, sys, time, urllib.request
from datetime import datetime, timezone
import numpy as np
import pandas as pd

OUT = sys.argv[1] if len(sys.argv) > 1 else "tape_events.csv"
WIN, BACKSTOP = 96, 32
VOL_MULT, RV_PCTILE, COST_BPS = 5.0, 0.60, 3.0
START, END = "2026-07-21", "2026-08-10"


def post(b, tries=6):
    for k in range(tries):
        try:
            r = urllib.request.Request("https://api.hyperliquid.xyz/info",
                                       data=json.dumps(b).encode(),
                                       headers={"Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(r, timeout=30))
        except Exception:
            time.sleep(min(20, 2 ** k))
    return None


def ms(d):
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
               .timestamp() * 1000)


t0, t1 = ms(START) - WIN * 900000, ms(END) + 900000
m = post({"type": "metaAndAssetCtxs"})
uni = {u["name"]: (u, c) for u, c in zip(m[0]["universe"], m[1])
       if c.get("midPx") is not None}
names = sorted(uni)
# tier by 24h notional, tertiles -- matches the bot's own universe split
dv = {s: float(uni[s][1].get("dayNtlVlm", 0) or 0) for s in names}
vals = sorted(v for v in dv.values() if v > 0)
b1, b2 = vals[len(vals)//3], vals[2*len(vals)//3]
tier = {s: ("LOW" if dv[s] < b1 else ("MID" if dv[s] < b2 else "HIGH")) for s in names}

print(f"fetching candles for {len(names)} perps ...")
cd = {}
for i, s in enumerate(names):
    d = post({"type": "candleSnapshot",
              "req": {"coin": s, "interval": "15m", "startTime": t0, "endTime": t1}})
    if d and len(d) > WIN + BACKSTOP:
        # NOTE: "T" is the bar CLOSE (open + 899999); "t" is the OPEN. Using T as the
        # bar timestamp puts every downstream window a full bar late, which silently
        # gave the flow features 15 minutes of lookahead the first time this was run.
        cd[s] = pd.DataFrame([{"t": int(c["t"]), "h": float(c["h"]), "l": float(c["l"]),
                               "c": float(c["c"]), "v": float(c["v"]),
                               "n": int(c["n"])} for c in d]).sort_values("t")
    if (i + 1) % 50 == 0:
        print(f"  {i+1}/{len(names)}")
    time.sleep(0.02)
print(f"got {len(cd)}")

print("fetching funding ...")
fund = {}
for s in cd:
    d = post({"type": "fundingHistory", "coin": s, "startTime": t0, "endTime": t1})
    if d:
        fund[s] = (np.array([int(x["time"]) for x in d]),
                   np.array([float(x["fundingRate"]) for x in d]))
    time.sleep(0.02)

rows = []
for s, g in cd.items():
    cl = g["c"].values; hi = g["h"].values; lo = g["l"].values
    vo = g["v"].values; nt = g["n"].values.astype(float); tm = g["t"].values
    med = pd.Series(vo).shift(1).rolling(WIN).median().values
    ph = pd.Series(hi).shift(1).rolling(WIN).max().values
    pl = pd.Series(lo).shift(1).rolling(WIN).min().values
    lr = np.full(len(cl), np.nan); lr[1:] = np.log(cl[1:] / cl[:-1])
    rv = pd.Series(lr).rolling(WIN).std().values
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = vo / med
        ats = vo / np.maximum(nt, 1)
        ats_r = ats / pd.Series(ats).shift(1).rolling(WIN).median().values
    brk = np.where(cl > ph, 1, np.where(cl < pl, -1, 0))
    ft, fr = fund.get(s, (None, None))
    for i in np.nonzero((vr >= VOL_MULT) & (brk != 0) & ~np.isnan(rv))[0]:
        if i + BACKSTOP >= len(cl) or tm[i] < ms(START):
            continue
        b = int(brk[i]); d = -b; entry = cl[i]
        aligned = False
        if ft is not None and len(ft):
            j = np.searchsorted(ft, tm[i], side="right") - 1
            if j >= 0:
                aligned = (1 if fr[j] > 0 else (-1 if fr[j] < 0 else 0)) == b
        ret = None
        for k in range(1, BACKSTOP + 1):
            c = cl[i + k]
            if (d < 0 and c < ph[i]) or (d > 0 and c > pl[i]):
                ret = d * (c - entry) / entry; bars = k; break
        why = "reclaim" if ret is not None else "backstop"
        if ret is None:
            ret = d * (cl[i + BACKSTOP] - entry) / entry; bars = BACKSTOP
        rows.append(dict(
            sym=s, t=int(tm[i]), bar_close_ms=int(tm[i]) + 900000, dirn=b,
            tier=tier.get(s, ""), rv=float(rv[i]), vratio=float(vr[i]),
            ats_ratio=float(ats_r[i]) if np.isfinite(ats_r[i]) else np.nan,
            entry=float(entry), prior_h=float(ph[i]), prior_l=float(pl[i]),
            aligned=int(aligned), why=why, bars=int(bars),
            fade_bps=float(ret * 1e4 - COST_BPS),
            **{f"fwd{h}": float(b * (cl[i+h] - entry) / entry * 1e4)
               for h in (2, 4, 8, 16, 32) if i + h < len(cl)}))

ev = pd.DataFrame(rows).sort_values("t").reset_index(drop=True)
ev["rv_pass"] = (ev.rv >= ev.rv.quantile(RV_PCTILE)).astype(int)
ev["tier_pass"] = ev.tier.isin(["HIGH", "MID"]).astype(int)
ev["signalled"] = (ev.aligned & ev.rv_pass & ev.tier_pass).astype(int)
ev.to_csv(OUT, index=False)
print(f"\nwrote {len(ev):,} events to {OUT}")
print(f"  signalled (all gates): {int(ev.signalled.sum()):,}")
print(f"  fade baseline, all events  {ev.fade_bps.mean():+.1f} bps  "
      f"backstop {100*(ev.why=='backstop').mean():.0f}%")
sg = ev[ev.signalled == 1]
print(f"  fade baseline, signalled   {sg.fade_bps.mean():+.1f} bps  "
      f"backstop {100*(sg.why=='backstop').mean():.0f}%")
print(f"  date range {datetime.utcfromtimestamp(ev.t.min()/1000):%Y-%m-%d} to "
      f"{datetime.utcfromtimestamp(ev.t.max()/1000):%Y-%m-%d}")
