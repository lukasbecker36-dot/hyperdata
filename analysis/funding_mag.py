#!/usr/bin/env python3
"""Add |funding| as a gate, and test it OUT OF SAMPLE.

analysis/backstop_filter.py found the strongest single number in this investigation:
|funding| top quintile earned +104.6bps/trade at t=+8.2 against a +45.6 baseline. The live
arm gates on funding's SIGN only and throws the magnitude away.

But that was found on the 15m/52-day sample, so re-testing it there proves nothing. This
runs the same test on the 1h/211-day history (Dec 2025 - Jul 2026), of which the first
~158 days predate the 15m window entirely and are genuinely unseen. Results are also
broken out by month, because the README's own finding is that the raw signal is
non-stationary and only worked Jun-Jul.

Gates replicate the live arm at whatever bar size is given: 5x volume spike, 24h range
breakout, rv above the 60th pct of signal rv, funding SIGN aligned, HIGH+MID tier. Exits
replicate it too: reclaim on the close back inside the prior range, else the 8h backstop.

Then |funding| is layered on as an extra gate at several thresholds. Reported per-trade AND
in total, plus trades/day -- a gate that doubles per-trade edge but trades a tenth as often
may still be right for a 5-slot bot, or may simply starve it.

  python3 analysis/funding_mag.py [15m|1h]
"""
import sys
import numpy as np
import pandas as pd

IV = sys.argv[1] if len(sys.argv) > 1 else "1h"
CFG = {"15m": ("hyperliquid_15m_allperps.csv", 96, 32, 1500),
       "1h":  ("hyperliquid_1h_history.csv", 24, 8, 400)}
CANDLES, WIN, BACKSTOP, MINBARS = CFG[IV]
VOL_MULT, RV_PCTILE, COST_BPS = 5.0, 0.60, 3.0
OOS_CUT = pd.Timestamp("2026-05-26", tz="UTC").value // 10**6   # 15m sample starts here

print(f"interval={IV}  win={WIN}  backstop={BACKSTOP} bars\nloading ...")
df = pd.read_csv(CANDLES).sort_values(["symbol", "open_time_ms"]).reset_index(drop=True)
uni = pd.read_csv("perp_universe.csv").set_index("name")["day_notional_vol"]
qs = uni.quantile([1/3, 2/3]).values
fund = pd.read_csv("hyperliquid_funding.csv").sort_values(["symbol", "time_ms"])
fmap = {s: (g["time_ms"].values, g["funding_rate"].values)
        for s, g in fund.groupby("symbol", sort=False)}

ev = []
for sym, g in df.groupby("symbol", sort=False):
    if len(g) < MINBARS:
        continue
    v = uni.get(sym, 0)
    if not (qs[0] <= v):            # HIGH+MID only (drop LOW)
        continue
    g = g.reset_index(drop=True)
    cl = g["close"].values.astype(float)
    hi = g["high"].values.astype(float)
    lo = g["low"].values.astype(float)
    vo = g["volume"].values.astype(float)
    tm = g["open_time_ms"].values
    med = pd.Series(vo).shift(1).rolling(WIN).median().values
    ph = pd.Series(hi).shift(1).rolling(WIN).max().values
    pl = pd.Series(lo).shift(1).rolling(WIN).min().values
    lr = np.full(len(cl), np.nan); lr[1:] = np.log(cl[1:]/cl[:-1])
    rv = pd.Series(lr).rolling(WIN).std().values
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = vo/med
    brk = np.where(cl > ph, 1, np.where(cl < pl, -1, 0))
    ft, fr = fmap.get(sym, (None, None))
    if ft is None:
        continue
    cand = np.nonzero((vr >= VOL_MULT) & (brk != 0) & ~np.isnan(rv))[0]
    for i in cand:
        if i + BACKSTOP >= len(cl):
            continue
        j = np.searchsorted(ft, tm[i], side="right") - 1
        if j < 0:
            continue
        f = fr[j]
        if (1 if f > 0 else (-1 if f < 0 else 0)) != brk[i]:
            continue
        d = -int(brk[i]); entry = cl[i]
        why, ret = "backstop", None
        for k in range(1, BACKSTOP+1):
            c = cl[i+k]
            if (d < 0 and c < ph[i]) or (d > 0 and c > pl[i]):
                why, ret = "reclaim", d*(c-entry)/entry
                break
        if ret is None:
            ret = d*(cl[i+BACKSTOP]-entry)/entry
        ev.append((sym, tm[i], rv[i], vr[i], abs(f)*1e6, why, ret*1e4))
ev = pd.DataFrame(ev, columns=["sym", "t", "rv", "vr", "fabs", "why", "ret"])
print(f"raw events: {len(ev):,}")
thr = ev["rv"].quantile(RV_PCTILE)
ev = ev[ev["rv"] >= thr].reset_index(drop=True)
ev["net"] = ev["ret"] - COST_BPS
days = (ev["t"].max() - ev["t"].min()) / 86400000
print(f"after rv gate: {len(ev):,} over {days:.0f} days\n")

print("=== |funding| distribution (ppm) -- note the clamp ===")
for q in (10, 25, 40, 50, 60, 75, 80, 90, 95, 99):
    print(f"  p{q:<3} {ev['fabs'].quantile(q/100):>10.2f}")
clamp = ev["fabs"].mode().iloc[0]
print(f"  most common value: {clamp:.2f} ppm on "
      f"{(ev['fabs'] == clamp).mean()*100:.0f}% of events  <- the clamped default\n")


def report(name, sub, base_total=None):
    if len(sub) < 25:
        print(f"  {name:>30} n={len(sub):<5} too few"); return None
    m, sd = sub["net"].mean(), sub["net"].std()
    t = m/(sd/np.sqrt(len(sub)))
    bs = (sub["why"] == "backstop").mean()*100
    tot = sub["net"].sum()
    frac = f"{tot/base_total*100:>5.0f}%" if base_total else "  base"
    print(f"  {name:>30} {len(sub):>6} {len(sub)/days:>7.1f} {m:>+8.1f} {t:>+6.1f} "
          f"{bs:>7.0f}% {tot:>+10,.0f} {frac}")
    return tot


def block(pool, label):
    print(f"\n--- {label}  (n={len(pool):,}) ---")
    print(f"  {'gate':>30} {'n':>6} {'/day':>7} {'bps':>8} {'t':>6} "
          f"{'stop%':>8} {'total':>10} {'%base'}")
    base = report("none (sign gate only)", pool)
    if base is None:
        return
    report("|f| > clamp", pool[pool["fabs"] > clamp*1.001], base)
    for p in (0.60, 0.80, 0.90):
        c = pool["fabs"].quantile(p)
        report(f"|f| >= p{int(p*100)} ({c:.1f}ppm)", pool[pool["fabs"] >= c], base)


block(ev, f"FULL SAMPLE ({IV})")

if IV == "1h":
    oos = ev[ev["t"] < OOS_CUT]
    ins = ev[ev["t"] >= OOS_CUT]
    block(oos, "TRUE OUT OF SAMPLE (before 2026-05-26, unseen by the 15m study)")
    block(ins, "IN-SAMPLE OVERLAP (2026-05-26 onward)")

    print("\n=== month by month: does the funding edge persist? ===")
    ev["mo"] = pd.to_datetime(ev["t"], unit="ms", utc=True).dt.strftime("%Y-%m")
    print(f"  {'month':>9} {'n':>5} {'all bps':>9} {'n hi-f':>7} {'hi-f bps':>10} {'edge':>8}")
    hi = ev["fabs"] >= ev["fabs"].quantile(0.80)
    for mo, g in ev.groupby("mo"):
        gh = g[hi.loc[g.index]]
        a = g["net"].mean()
        h = gh["net"].mean() if len(gh) >= 8 else float("nan")
        print(f"  {mo:>9} {len(g):>5} {a:>+9.1f} {len(gh):>7} "
              f"{h:>+10.1f} {h-a:>+8.1f}" if len(gh) >= 8 else
              f"  {mo:>9} {len(g):>5} {a:>+9.1f} {len(gh):>7} {'-':>10} {'-':>8}")

print("\n'%base' is what fraction of the ungated total P&L the gate retains. A gate that")
print("keeps 40% of the P&L on 20% of the trades has doubled per-trade edge -- good for a")
print("5-slot bot, bad if you had capital for all of them. 'edge' in the monthly table is")
print("the high-funding subset minus that month's average: it must be positive in most")
print("months, not just on average, or it is one regime wearing a disguise.")
