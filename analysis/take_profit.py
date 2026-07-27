#!/usr/bin/env python3
"""Would a take-profit beat holding to reclaim / the 8h backstop, on the 15m-ats arm?

Distinct from the stop-loss question already settled. README: stops DESTROY the edge --
the negative skew is the risk premium being collected. A take-profit is the opposite
trade-off: it caps the favourable tail instead of the adverse one. Whether that helps
depends entirely on whether the fade OVERSHOOTS and gives profit back before the
reclaim/backstop exit fires.

Baseline replicates the live arm's exits exactly:
    reclaim   the bar CLOSES back inside the prior 24h range (short: close < prior_high,
              long: close > prior_low)
    backstop  32 bars = 8h, whichever comes first. No stop-loss.

Variants add a take-profit and keep everything else. Two fill assumptions, because it
matters a lot and today's audit showed why:
    wick   the bar's favourable EXTREME reaches the level -> filled. Optimistic: a limit
           order touched intrabar may not fill, exactly the problem shadow_fill found.
    close  the bar CLOSES past the level. Conservative, and closer to what a maker order
           resting through a whole bar would realistically get.

Gates replicate the live arm: 5x volume spike, 24h range breakout, realized vol above
the 60th percentile of signal rv, funding sign aligned with the breakout, HIGH+MID tier.

Note the comparison is in BPS PER TRADE, so it is independent of the ats size multiplier
-- sizing changes the dollar weighting, not whether a TP improves a given trade.

  python3 analysis/take_profit.py
"""
import numpy as np
import pandas as pd

VOLWIN = RANGEWIN = RVWIN = 96      # 24h at 15m
VOL_MULT = 5.0
RV_PCTILE = 0.60
BACKSTOP = 32                       # 8h
COST_BPS = 3.0                      # maker round trip
TPS = [25, 50, 75, 100, 150, 200, 300, 500]
MINBARS = 1500

print("loading ...")
df = pd.read_csv("hyperliquid_15m_allperps.csv").sort_values(
    ["symbol", "open_time_ms"]).reset_index(drop=True)
uni = pd.read_csv("perp_universe.csv").set_index("name")["day_notional_vol"]
qs = uni.quantile([1/3, 2/3]).values
tier_of = lambda v: "LOW" if v < qs[0] else ("MID" if v < qs[1] else "HIGH")

fund = pd.read_csv("hyperliquid_funding.csv").sort_values(["symbol", "time_ms"])

# ---- build signals ----
ev = []
paths = {}
for sym, g in df.groupby("symbol", sort=False):
    if len(g) < MINBARS:
        continue
    if tier_of(uni.get(sym, 0)) not in ("HIGH", "MID"):
        continue
    g = g.reset_index(drop=True)
    close = g["close"].values.astype(float)
    high = g["high"].values.astype(float)
    low = g["low"].values.astype(float)
    tms = g["open_time_ms"].values
    med = pd.Series(g["volume"]).shift(1).rolling(VOLWIN).median().values
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = g["volume"].values / med
    ph = pd.Series(high).shift(1).rolling(RANGEWIN).max().values
    pl = pd.Series(low).shift(1).rolling(RANGEWIN).min().values
    lret = np.full(len(close), np.nan)
    lret[1:] = np.log(close[1:] / close[:-1])
    rv = pd.Series(lret).rolling(RVWIN).std().values
    brk = np.where(close > ph, 1, np.where(close < pl, -1, 0))
    paths[sym] = (close, high, low, ph, pl)
    # funding sign at each signal, via merge_asof on the symbol's funding series
    fs = fund[fund.symbol == sym]
    for i in np.nonzero((vr >= VOL_MULT) & (brk != 0) & ~np.isnan(rv))[0]:
        if i + 1 >= len(close):
            continue
        ev.append((sym, i, tms[i], -int(brk[i]), int(brk[i]), float(rv[i]), float(vr[i])))
ev = pd.DataFrame(ev, columns=["sym", "i", "t", "fade", "brk", "rv", "vr"])
print(f"raw spike+breakout events: {len(ev):,}")

# rv gate: 60th percentile of SIGNAL rv, matching how the bot calibrates
thr = ev["rv"].quantile(RV_PCTILE)
ev = ev[ev["rv"] >= thr].copy()
print(f"after rv >= {RV_PCTILE:.0%}ile of signal rv ({thr:.5f}): {len(ev):,}")

# funding gate: breakout direction must match funding sign
fsign = {}
for sym, fg in fund.groupby("symbol", sort=False):
    fsign[sym] = (fg["time_ms"].values, np.sign(fg["funding_rate"].values))
keep = []
for r in ev.itertuples():
    a = fsign.get(r.sym)
    if a is None:
        keep.append(False); continue
    j = np.searchsorted(a[0], r.t, side="right") - 1
    keep.append(bool(j >= 0 and a[1][j] == r.brk))
ev = ev[np.array(keep)].copy()
print(f"after funding alignment: {len(ev):,}\n")
if len(ev) < 200:
    raise SystemExit("too few events")


def simulate(tp_bps, mode):
    """Return (ret_bps, bars_held, exit_reason) arrays. tp_bps=None -> baseline."""
    out_r, out_k, out_why, out_mfe = [], [], [], []
    tp = None if tp_bps is None else tp_bps / 1e4
    for r in ev.itertuples():
        close, high, low, ph, pl = paths[r.sym]
        i, d = r.i, r.fade
        if i + BACKSTOP >= len(close):
            continue
        entry = close[i]
        prior_h, prior_l = ph[i], pl[i]
        mfe = 0.0
        done = None
        for k in range(1, BACKSTOP + 1):
            # favourable extreme this bar (short profits as price falls)
            fav = (entry - low[i+k]) / entry if d < 0 else (high[i+k] - entry) / entry
            mfe = max(mfe, fav)
            if tp is not None:
                hit = fav >= tp if mode == "wick" else \
                      (d * (close[i+k] - entry) / entry) >= tp
                if hit:
                    done = (tp, k, "tp")
                    break
            # reclaim, on the close, exactly as the bot tests it
            c = close[i+k]
            if (d < 0 and c < prior_h) or (d > 0 and c > prior_l):
                done = (d * (c - entry) / entry, k, "reclaim")
                break
        if done is None:
            c = close[i+BACKSTOP]
            done = (d * (c - entry) / entry, BACKSTOP, "backstop")
        out_r.append(done[0] * 1e4); out_k.append(done[1])
        out_why.append(done[2]); out_mfe.append(mfe * 1e4)
    return (np.array(out_r), np.array(out_k), np.array(out_why), np.array(out_mfe))


base_r, base_k, base_why, base_mfe = simulate(None, "wick")
n = len(base_r)
print(f"=== BASELINE: reclaim or 8h backstop, no take-profit  (n={n:,}) ===")
print(f"  gross {base_r.mean():+.1f}bps   net {base_r.mean()-COST_BPS:+.1f}bps   "
      f"win {(base_r > COST_BPS).mean()*100:.0f}%   avg hold {base_k.mean()*15/60:.1f}h")
for w in ("reclaim", "backstop"):
    m = base_why == w
    print(f"    {w:>9}: {m.sum():>5} ({m.mean()*100:>2.0f}%)  "
          f"mean {base_r[m].mean():+7.1f}bps")

print(f"\n=== THE GIVE-BACK: how much of the best price is handed back? ===")
print(f"  mean max favourable excursion : {base_mfe.mean():+.1f} bps")
print(f"  mean realised exit            : {base_r.mean():+.1f} bps")
print(f"  mean given back               : {base_mfe.mean()-base_r.mean():+.1f} bps")
for q in (25, 50, 75, 90):
    print(f"    MFE p{q}: {np.percentile(base_mfe, q):>7.1f} bps")
print("  (a TP can only ever harvest give-back; if MFE is barely above the realised exit,")
print("   there is nothing to capture and a TP can only truncate winners)")

for mode in ("wick", "close"):
    print(f"\n=== TAKE-PROFIT variants -- fill assumption: {mode.upper()} ===")
    print(f"  {'TP':>6} {'gross':>8} {'net':>8} {'win%':>6} {'hold h':>7} "
          f"{'tp%':>5} {'reclaim%':>9} {'stop%':>6} {'vs base':>8} {'mean/sd':>8}")
    bn = base_r.mean() - COST_BPS
    print(f"  {'none':>6} {base_r.mean():>+8.1f} {bn:>+8.1f} "
          f"{(base_r > COST_BPS).mean()*100:>5.0f}% {base_k.mean()*15/60:>7.1f} "
          f"{'-':>5} {(base_why=='reclaim').mean()*100:>8.0f}% "
          f"{(base_why=='backstop').mean()*100:>5.0f}% {'-':>8} "
          f"{(base_r-COST_BPS).mean()/(base_r-COST_BPS).std():>8.3f}")
    for tp in TPS:
        r, k, why, _ = simulate(tp, mode)
        net = r - COST_BPS
        print(f"  {tp:>6} {r.mean():>+8.1f} {net.mean():>+8.1f} "
              f"{(r > COST_BPS).mean()*100:>5.0f}% {k.mean()*15/60:>7.1f} "
              f"{(why=='tp').mean()*100:>4.0f}% {(why=='reclaim').mean()*100:>8.0f}% "
              f"{(why=='backstop').mean()*100:>5.0f}% {net.mean()-bn:>+8.1f} "
              f"{net.mean()/net.std():>8.3f}")

print("\n'vs base' is the whole answer, in bps per trade. 'mean/sd' is per-trade Sharpe --")
print("a TP that trades a little return for much less variance can still be worth it if")
print("you are capacity-constrained, which the live arm is (5 slots, $250 gross).")
