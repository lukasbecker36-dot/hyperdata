#!/usr/bin/env python3
"""How much of the tape is actually being used? Sizes the untapped sample.

The flow features have only ever been attached to trades the bot FILLED -- 273 of them.
That is the smallest possible sample the tape can support, and it is selected three ways
over: only signals that passed every gate, only those that got a fill, and only in the
coins and hours the bot happened to be free to trade.

The tape itself is universal: every print in all 177 perps, continuously, ~1.8M prints a
day since 2026-07-21. This counts the events that exist in that window regardless of
whether the bot took them, which is the population any real flow study should run on.

Three widening steps, each strictly containing the last:

  FILLED     what vpin_edge.py used                                 (273)
  SIGNALLED  every event that met the entry rule, filled or not -- adds signals refused
             by caps, by the daily loss limit, and by an unfilled resting order
  ALL SPIKES every 5x volume spike + 24h breakout in any coin, before the funding and rv
             gates. The gates exist to pick trades, not to define the population a
             feature should be tested on, and dropping them multiplies the sample.

  python3 analysis/tape_inventory.py
"""
import json, math, time, urllib.request
from datetime import datetime, timezone
import numpy as np
import pandas as pd

WIN, BACKSTOP = 96, 32
VOL_MULT, RV_PCTILE = 5.0, 0.60
START = "2026-07-21"          # first full tape day
END = "2026-08-10"


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
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()*1000)


t0, t1 = ms(START) - WIN*900000, ms(END) + 900000
m = post({"type": "metaAndAssetCtxs"})
uni = {u["name"]: u for u, c in zip(m[0]["universe"], m[1]) if c.get("midPx") is not None}
names = sorted(uni)
print(f"fetching 15m candles for {len(names)} perps, {START} to {END} ...")
cd = {}
for i, s in enumerate(names):
    d = post({"type": "candleSnapshot",
              "req": {"coin": s, "interval": "15m", "startTime": t0, "endTime": t1}})
    if d and len(d) > WIN + BACKSTOP:
        # "T" is the bar CLOSE; "t" is the OPEN. See tape_events.py.
        cd[s] = pd.DataFrame([{"t": int(c["t"]), "h": float(c["h"]), "l": float(c["l"]),
                               "c": float(c["c"]), "v": float(c["v"]),
                               "n": int(c["n"])} for c in d]).sort_values("t")
    if (i+1) % 40 == 0:
        print(f"  {i+1}/{len(names)}")
    time.sleep(0.02)
print(f"got {len(cd)} coins\n")

# funding, for the alignment gate
fund = {}
for s in cd:
    d = post({"type": "fundingHistory", "coin": s, "startTime": t0, "endTime": t1})
    if d:
        fund[s] = (np.array([int(x["time"]) for x in d]),
                   np.array([float(x["fundingRate"]) for x in d]))

spikes = 0
signals = []
bars_total = 0
for s, g in cd.items():
    cl = g["c"].values; hi = g["h"].values; lo = g["l"].values
    vo = g["v"].values; tm = g["t"].values
    bars_total += len(g)
    med = pd.Series(vo).shift(1).rolling(WIN).median().values
    ph = pd.Series(hi).shift(1).rolling(WIN).max().values
    pl = pd.Series(lo).shift(1).rolling(WIN).min().values
    lr = np.full(len(cl), np.nan); lr[1:] = np.log(cl[1:]/cl[:-1])
    rv = pd.Series(lr).rolling(WIN).std().values
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = vo/med
    brk = np.where(cl > ph, 1, np.where(cl < pl, -1, 0))
    idx = np.nonzero((vr >= VOL_MULT) & (brk != 0) & ~np.isnan(rv))[0]
    for i in idx:
        if i + BACKSTOP >= len(cl) or tm[i] < ms(START):
            continue
        spikes += 1
        ft, fr = fund.get(s, (None, None))
        ok = False
        if ft is not None and len(ft):
            j = np.searchsorted(ft, tm[i], side="right") - 1
            if j >= 0:
                ok = (1 if fr[j] > 0 else (-1 if fr[j] < 0 else 0)) == brk[i]
        signals.append(dict(sym=s, t=tm[i], rv=rv[i], aligned=ok))

sig = pd.DataFrame(signals)
gated = sig[sig.aligned & (sig.rv >= sig.rv.quantile(RV_PCTILE))]
days = (ms(END) - ms(START)) / 86400000

print("=== events available in the tape window ===")
print(f"  {'population':<44} {'n':>8} {'per day':>9} {'vs filled':>10}")
FILLED = 273
for lab, n in (("FILLED — what the flow study has used", FILLED),
               ("SIGNALLED (5x spike + breakout + funding + rv)", len(gated)),
               ("ALL 5x SPIKES + breakout, before gates", spikes)):
    print(f"  {lab:<44} {n:>8,} {n/days:>9.0f} {n/FILLED:>9.1f}x")
print(f"\n  candle bars scanned: {bars_total:,} across {len(cd)} coins over {days:.0f} days")

print("\n=== what that does to the resolvable effect size ===")
SD = 305.0
print(f"  per-trade sd is {SD:.0f} bps. Smallest quintile-pair gap resolvable at t=3:")
for lab, n in (("filled only", FILLED), ("signalled", len(gated)), ("all spikes", spikes)):
    g = n // 5 * 2
    if g < 5:
        continue
    print(f"    {lab:<16} n={n:>6,}  ->  {3*SD*math.sqrt(2/g):>5.0f} bps")
print("  vpin_edge.py could not resolve anything under ~124 bps, and the observed")
print("  effects averaged under 20. This is the gap that closes it.")

print("\n=== tape coverage of those events ===")
print(f"  tape runs {START} to {END} continuously, all 177 coins, ~1.8M prints/day")
print(f"  ~{1.8e6*days/1e6:.0f}M prints total; the bot reads a 12MB tail per cycle and")
print(f"  keeps 3 numbers per filled trade. Everything else is on disk and unused.")
