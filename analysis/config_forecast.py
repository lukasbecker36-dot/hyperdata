#!/usr/bin/env python3
"""What the CURRENT live config should do: P&L, ROI, and peak margin.

Four changes went in over three days and they interact, so arithmetic on each in isolation
is not an answer:

  --notional 24 with ats x pierce sizing, product capped at 4.0x
  --rv-pctile 0.40      (rolling 15d rank, was 0.60)
  LIQ_SIGMA 6.0         (leverage per coin so the liquidation cushion covers 6 sigma)
  caps 40 positions / 20 per side / $2000 gross / -$15 daily loss

This simulates the whole thing on the 15m event set: rolling rv gate, rolling pierce rank,
per-coin leverage from the real maxLeverage table, position book with the actual
reclaim/backstop exits, and every cap enforced in order. It reports the margin TIME SERIES,
because peak margin is the number that decides whether the config is fundable and no
closed-form expression for it exists once caps and clustering are involved.

Two P&L estimates are given deliberately and they differ by 10x:

  BACKTEST   what the event set says, May-Jul, frictionless fills
  LIVE-CAL   the same simulation rescaled to the bps/trade actually realised on 307 live
             fills, which is the honest forward estimate

  python3 analysis/config_forecast.py
"""
import json, math, time, urllib.request
import numpy as np
import pandas as pd

WIN, BACKSTOP, VOL_MULT, COST = 96, 32, 5.0, 3.0
BASE, SIZE_REF, SIZE_MIN, SIZE_MAX = 24.0, 2.0, 0.5, 3.0
PIERCE_REF, PIERCE_MAX, MULT_MAX = 0.5, 2.0, 4.0
LIQ_SIGMA, LEV_CAP = 6.0, 3
RV_PCT, CALIB_MS = 0.40, 15 * 86400000
MAX_POS, MAX_SIDE, MAX_GROSS, DAILY_LOSS = 40, 20, 2000.0, 15.0
SPOT = 383.0


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


meta = post({"type": "meta"})
maxlev = {u["name"]: int(u["maxLeverage"]) for u in meta["universe"]}
uni = pd.read_csv("perp_universe.csv").set_index("name")["day_notional_vol"]
qs = uni.quantile([1/3, 2/3]).values
tier = {s: ("LOW" if v < qs[0] else ("MID" if v < qs[1] else "HIGH")) for s, v in uni.items()}
fund = pd.read_csv("hyperliquid_funding.csv").sort_values(["symbol", "time_ms"])
fmap = {s: (g["time_ms"].values, g["funding_rate"].values)
        for s, g in fund.groupby("symbol", sort=False)}

print("building events ...")
df = pd.read_csv("hyperliquid_15m_allperps.csv").sort_values(
    ["symbol", "open_time_ms"]).reset_index(drop=True)
rows = []
for sym, g in df.groupby("symbol", sort=False):
    if len(g) < 1500 or tier.get(sym, "LOW") == "LOW":
        continue
    g = g.reset_index(drop=True)
    cl = g["close"].values.astype(float); hi = g["high"].values.astype(float)
    lo = g["low"].values.astype(float); vo = g["volume"].values.astype(float)
    nt = g["num_trades"].values.astype(float); tm = g["open_time_ms"].values
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
    ft, fr = fmap.get(sym, (None, None))
    if ft is None:
        continue
    for i in np.nonzero((vr >= VOL_MULT) & (brk != 0) & ~np.isnan(rv))[0]:
        if i + BACKSTOP >= len(cl) or not np.isfinite(ar[i]):
            continue
        j = np.searchsorted(ft, tm[i], side="right") - 1
        if j < 0 or (1 if fr[j] > 0 else (-1 if fr[j] < 0 else 0)) != brk[i]:
            continue
        b = int(brk[i]); d = -b; e = cl[i]
        pierce = (e - ph[i])/ph[i] if b > 0 else (pl[i] - e)/pl[i]
        ret, bars = None, BACKSTOP
        for k in range(1, BACKSTOP+1):
            c = cl[i+k]
            if (d < 0 and c < ph[i]) or (d > 0 and c > pl[i]):
                ret, bars = d*(c-e)/e, k; break
        if ret is None:
            ret = d*(cl[i+BACKSTOP]-e)/e
        rows.append(dict(t=int(tm[i]), sym=sym, dirn=b, rv=float(rv[i]), ats=float(ar[i]),
                         pierce=float(pierce), bars=int(bars),
                         net=float(ret*1e4 - COST), maxlev=maxlev.get(sym, 3)))
ev = pd.DataFrame(rows).sort_values("t").reset_index(drop=True)
n = len(ev)

# rolling ranks, exactly as the bot calibrates
t = ev.t.values; rvv = ev.rv.values; pv = ev.pierce.values
rvp = np.full(n, np.nan); pp = np.full(n, np.nan)
for i in range(n):
    a = np.searchsorted(t, t[i] - CALIB_MS, "left")
    if i - a >= 200:
        rvp[i] = (rvv[a:i] < rvv[i]).mean()
        pp[i] = (pv[a:i] < pv[i]).mean()
ev["rvp"] = rvp; ev["pp"] = pp
ev = ev[ev.rvp.notna() & (ev.rvp >= RV_PCT)].reset_index(drop=True)
days = (ev.t.max()-ev.t.min())/86400000
print(f"  {len(ev):,} signals pass the 40th-pct rv gate over {days:.0f} days "
      f"({len(ev)/days:.1f}/day)")

# sizing and leverage, per the live rules
am = np.clip(ev.ats.values/SIZE_REF, SIZE_MIN, SIZE_MAX)
pm = np.clip(ev.pp.values/PIERCE_REF, SIZE_MIN, PIERCE_MAX)
ev["mult"] = np.clip(am*pm, SIZE_MIN, MULT_MAX)
ev["ntl"] = BASE*ev["mult"]
mm = 1.0/(2*ev.maxlev.values)
sig = ev.rv.values*math.sqrt(BACKSTOP)
room = LIQ_SIGMA*sig*(1+mm) + mm
ev["lev"] = np.clip((1.0/room).astype(int), 1, LEV_CAP)
ev["margin"] = ev.ntl/ev.lev
print(f"  mean notional ${ev.ntl.mean():.2f}  mean leverage {ev.lev.mean():.2f}x  "
      f"mean margin ${ev.margin.mean():.2f}")

# ---- book simulation with every cap, in the bot's order ----
open_pos = []          # (exit_ms, dirn, ntl, margin)
taken, skipped = [], 0
day_pnl, cur_day = 0.0, None
series = []
for r in ev.itertuples():
    open_pos = [p for p in open_pos if p[0] > r.t]
    d = pd.to_datetime(r.t, unit="ms").date()
    if d != cur_day:
        cur_day, day_pnl = d, 0.0
    gross = sum(p[2] for p in open_pos)
    same = sum(1 for p in open_pos if p[1] == r.dirn)
    if (len(open_pos) >= MAX_POS or same >= MAX_SIDE
            or gross + r.ntl > MAX_GROSS or day_pnl <= -DAILY_LOSS):
        skipped += 1
        continue
    pnl = r.ntl*r.net/1e4
    day_pnl += pnl
    taken.append(dict(t=r.t, pnl=pnl, net=r.net, ntl=r.ntl, margin=r.margin,
                      lev=r.lev, dirn=r.dirn))
    open_pos.append((r.t + r.bars*900000, r.dirn, r.ntl, r.margin))
    series.append((r.t, sum(p[3] for p in open_pos), sum(p[2] for p in open_pos),
                   len(open_pos)))
T = pd.DataFrame(taken)
S = pd.DataFrame(series, columns=["t", "margin", "gross", "npos"])
print(f"  taken {len(T):,}  refused by caps {skipped:,}\n")

pnl = T.pnl.sum()
mu_bps = T.net.mean()
peak_m, avg_m = S.margin.max(), S.margin.mean()
print("=" * 74)
print("### CURRENT CONFIG — simulated on the 15m event set (May-Jul)")
print("=" * 74)
print(f"  trades                 {len(T):,}  ({len(T)/days:.1f}/day)")
print(f"  bps per trade          {mu_bps:+.1f}")
print(f"  total P&L              ${pnl:+,.2f}  over {days:.0f} days "
      f"(${pnl/days:+.2f}/day)")
print(f"\n  MARGIN")
print(f"    average             ${avg_m:>8.2f}   ({100*avg_m/SPOT:.0f}% of ${SPOT:.0f} spot)")
print(f"    peak                ${peak_m:>8.2f}   ({100*peak_m/SPOT:.0f}% of spot)")
print(f"    peak positions       {int(S.npos.max()):>8}   peak gross ${S.gross.max():,.0f}")
print(f"    p95 margin          ${S.margin.quantile(0.95):>8.2f}")
print(f"\n  ROI (per 30 days)")
for lab, den in (("average margin", avg_m), ("peak margin", peak_m),
                 ("spot collateral", SPOT)):
    print(f"    on {lab:<18} {100*pnl/den/days*30:>+7.1f}%")

print(f"\n{'='*74}")
print("### RESCALED TO LIVE-REALISED bps (the honest forward estimate)")
print("=" * 74)
LIVE_ALL, LIVE_EXLIQ = 2.5, 21.7
for lab, bps in (("live incl. 3 liquidations", LIVE_ALL),
                 ("live ex-liquidation", LIVE_EXLIQ)):
    p = len(T)*bps/1e4*T.ntl.mean()
    print(f"  {lab:<28} {bps:+5.1f} bps -> ${p/days*30:+7.2f}/30d   "
          f"{100*p/den/days*30 if False else 100*p/peak_m/days*30:+6.1f}% on peak margin")
print(f"\n  backtest says {mu_bps:+.1f} bps, live says {LIVE_ALL:+.1f} all-in and "
      f"{LIVE_EXLIQ:+.1f} ex-liquidation.")
print("  The gap is the whole uncertainty: frictionless candle exits vs real fills, and a")
print("  20-day live window that contained three liquidations.")
