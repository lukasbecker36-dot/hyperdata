#!/usr/bin/env python3
"""Intra-spike order flow as a SIZING TILT on trades we are taking anyway.

The cost floor (fc_verdict.md) killed flow as a standalone signal: 0.93bps of forecastable
move against a 2.88bps round trip. That verdict does not apply here. A tilt does not
create trades, it reallocates size across trades already being entered, so the round trip
is sunk and the bar drops from "beat 2.88bps" to merely "sort outcomes" -- the bar ats and
pierce both cleared.

What was already tested and was flat: vpin30/60, adverse_ofi, tape ats, size_conc,
big_share, prints_ratio (tape_features.py, 4,833 events, corr -0.036 to +0.003). Every one
of those is a TRAILING 30-60 minute window ending at the signal bar. None of them looks
inside the spike.

The hypothesis here is specifically about that inside: does the aggressive flow keep
pushing all the way to the close (informed continuation -> the fade gets run over), or does
it dry up and flip in the final minutes (exhaustion -> the fade reverts hard)? A 15m signal
bar contains 15 of the 1m bars in bars_1m.csv.gz, each carrying signed notional AND signed
big-print notional, so the shape is recoverable.

Features, all measured strictly inside [t, t+15m) and therefore known at the decision
moment, and all SIGNED so that positive = flow still running in the breakout direction =
continuation = the fade's enemy:

  flow_term3    OFI over the last 3 minutes of the spike bar
  flow_decay    OFI(last 5m) - OFI(first 5m). Negative = pressure fading through the bar
  big_term5     signed BIG-print flow (>=$10k) over the last 5 minutes. The bucket file's
                big_ntl is unsigned, which is exactly the half that matters here
  intens_decay  print count in the last 3m over the first 3m -- participation drying up
  px_flow_div   price still extending while flow has already flipped: the textbook
                exhaustion signature, and the one thing none of the earlier features could
                express

Judged as a tilt, not as a forecast: tercile spread, monthly/daily stability, both halves,
and finally return/|DD| at EQUAL average capital, which is the test pierce had to pass.

  python3 analysis/flow_tilt.py events.csv bars_1m.csv.gz
"""
import math, sys
import numpy as np
import pandas as pd

EV = sys.argv[1] if len(sys.argv) > 1 else "tape_events_featured.csv"
BARS = sys.argv[2] if len(sys.argv) > 2 else "bars_1m.csv.gz"
BAR_MS = 900000
NOT = 35.0

ev = pd.read_csv(EV)
print(f"{len(ev):,} fade events")
bars = pd.read_csv(BARS, dtype={"coin": "category"})
bars["ntl"] = bars.buy_ntl + bars.sell_ntl
bars["sntl"] = bars.buy_ntl - bars.sell_ntl
bars["sbig"] = bars.big_buy_ntl - bars.big_sell_ntl
bars = bars.sort_values(["coin", "bar_ms"], kind="mergesort").reset_index(drop=True)
print(f"{len(bars):,} 1m bars")

# index by coin for fast slicing
idx = {}
for c, g in bars.groupby("coin", observed=True, sort=False):
    idx[str(c)] = (g.bar_ms.values, g.sntl.values, g.ntl.values,
                   g.sbig.values, g.n.values.astype(float), g.c.values, g.o.values)

rows = []
for r in ev.itertuples():
    p = idx.get(r.sym)
    if p is None:
        rows.append((np.nan,)*5); continue
    t, sn, nt_, sb, cnt, cl, op = p
    lo = np.searchsorted(t, r.t, "left")
    hi = np.searchsorted(t, r.t + BAR_MS, "left")
    if hi - lo < 8:                      # need most of the bar populated to say anything
        rows.append((np.nan,)*5); continue
    d = float(r.dirn)                    # +1 = up-breakout (we fade short), -1 = down
    seg_sn, seg_nt, seg_sb = sn[lo:hi], nt_[lo:hi], sb[lo:hi]
    seg_cnt, seg_cl = cnt[lo:hi], cl[lo:hi]
    k = len(seg_sn)
    n3, n5 = max(2, k//5), max(3, k//3)

    def ofi(a, b):
        num = seg_sn[a:b].sum(); den = seg_nt[a:b].sum()
        return d * num / den if den > 0 else np.nan

    flow_term3 = ofi(k-n3, k)
    flow_decay = ofi(k-n5, k) - ofi(0, n5)
    bden = np.abs(seg_sb).sum()
    big_term5 = d * seg_sb[k-n5:].sum() / bden if bden > 0 else np.nan
    c0, c1 = seg_cnt[:n3].sum(), seg_cnt[k-n3:].sum()
    intens_decay = c1 / c0 if c0 > 0 else np.nan
    # price still extending in the breakout direction while flow has flipped against it
    px_move = d * (seg_cl[-1] - seg_cl[max(0, k-n5)]) / seg_cl[max(0, k-n5)]
    # continuous divergence: price extending (positive) while terminal flow opposes
    # (flow_term3 negative) gives a large positive value. Zeroing the non-divergent half
    # made this ~70% ties, which is not a feature, it is a flag.
    px_flow_div = (px_move * 1e4) * (-flow_term3 if flow_term3 == flow_term3 else np.nan)
    rows.append((flow_term3, flow_decay, big_term5, intens_decay, px_flow_div))

F = pd.DataFrame(rows, columns=["flow_term3", "flow_decay", "big_term5",
                                "intens_decay", "px_flow_div"])
ev = pd.concat([ev.reset_index(drop=True), F], axis=1)
FEATS = list(F.columns)
print("coverage: " + ", ".join(f"{f} {int(ev[f].notna().sum()):,}" for f in FEATS))
ev["day"] = pd.to_datetime(ev.t, unit="ms").dt.strftime("%m-%d")
ev["half"] = np.where(ev.t <= ev.t.median(), "1st", "2nd")


def st(v):
    v = np.asarray(v, float); v = v[~np.isnan(v)]
    n = len(v)
    return (v.mean(), v.mean()/(v.std(ddof=1)/math.sqrt(n)), n) if n > 1 else (np.nan,)*3


for pop_lab, pop in (("ALL SPIKES", ev), ("SIGNALLED (gated)", ev[ev.signalled == 1])):
    print(f"\n{'='*78}\n### {pop_lab} — n={len(pop):,}, baseline {pop.fade_bps.mean():+.1f} bps"
          f"\n{'='*78}")
    for f in FEATS:
        s = pop[pop[f].notna()].copy()
        if len(s) < 300:
            print(f"\n--- {f}: only {len(s)} usable"); continue
        try:
            s["q"] = pd.qcut(s[f], 3, labels=["LOW", "MID", "HIGH"], duplicates="drop")
        except ValueError:
            print(f"--- {f}: too many ties to form terciles "
                  f"({s[f].nunique()} distinct)")
            continue
        m = s.groupby("q", observed=True).fade_bps.agg(["mean", "size"])
        r = np.corrcoef(s[f], s.fade_bps)[0, 1]
        tr = r*math.sqrt((len(s)-2)/max(1e-12, 1-r*r))
        spread = m.loc["LOW", "mean"] - m.loc["HIGH", "mean"] if "HIGH" in m.index else np.nan
        print(f"\n--- {f}  (n={len(s):,})  corr {r:+.3f} t={tr:+.2f}")
        print("    " + "  ".join(f"{k}: {v['mean']:+7.1f} (n={int(v['size'])})"
                                 for k, v in m.iterrows()))
        # exhaustion hypothesis: LOW (flow flipped) should BEAT HIGH (flow still pushing)
        print(f"    LOW minus HIGH = {spread:+.1f} bps "
              f"({'supports exhaustion' if spread > 0 else 'opposite direction'})")
        h1 = s[s.half == "1st"]; h2 = s[s.half == "2nd"]
        for lab, sub in (("1st", h1), ("2nd", h2)):
            if len(sub) < 100:
                continue
            mm = sub.groupby("q", observed=True).fade_bps.mean()
            if "HIGH" in mm.index and "LOW" in mm.index:
                print(f"    {lab} half spread {mm['LOW']-mm['HIGH']:+.1f}")
        byday = s.groupby("day").apply(
            lambda g: (g[g.q == "LOW"].fade_bps.mean() - g[g.q == "HIGH"].fade_bps.mean())
            if (g.q == "LOW").sum() >= 3 and (g.q == "HIGH").sum() >= 3 else np.nan,
            include_groups=False).dropna()
        if len(byday) >= 8:
            print(f"    days with a positive spread: {int((byday > 0).sum())}/{len(byday)}")

# ------------------------------------------------------------------ tilt pricing
print(f"\n{'='*78}\n### PRICED AS A TILT, equal average capital\n{'='*78}")
g = ev[ev.signalled == 1].copy()
best = None
for f in FEATS:
    s = g[g[f].notna()].copy()
    if len(s) < 300:
        continue
    # rank-based tilt: low feature (flow flipped = exhaustion) -> bigger bet
    rk = 1.0 - s[f].rank(pct=True)
    mult = np.clip(rk/0.5, 0.5, 2.0)
    mult = mult/mult.mean()
    flat = NOT * s.fade_bps/1e4
    tilt = NOT * mult * s.fade_bps/1e4
    for lab, u in (("flat", flat), (f, tilt)):
        cum = np.cumsum(u.values)
        dd = float(np.min(cum - np.maximum.accumulate(cum)))
        if lab == "flat" and best is None:
            print(f"  {'rule':<16} {'n':>5} {'total $':>9} {'bps/trade':>10} {'ret/DD':>8}")
            print(f"  {'flat baseline':<16} {len(s):>5} {u.sum():>+9.2f} "
                  f"{s.fade_bps.mean():>+10.1f} {u.sum()/abs(dd) if dd<0 else float('nan'):>8.2f}")
            best = True
        elif lab != "flat":
            print(f"  {f:<16} {len(s):>5} {u.sum():>+9.2f} "
                  f"{float((s.fade_bps*mult).mean()):>+10.1f} "
                  f"{u.sum()/abs(dd) if dd<0 else float('nan'):>8.2f}")
print("\n  A tilt earns its place only if it lifts bps/trade AND return/|DD| at the same")
print("  average capital. Costs are sunk -- these are trades the bot already takes.")
