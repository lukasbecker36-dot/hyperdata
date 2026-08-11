#!/usr/bin/env python3
"""The two gaps flow_tilt.py left open: big-print coverage, and sub-minute structure.

GAP 1 -- COVERAGE. big_term5 had 255 usable gated observations because most spike bars
contain no $10k+ print. That is a coverage failure, not a null. spike_tape.py splits every
in-window print into size bands [0,1k) [1k,5k) [5k,20k) [20k,inf) with buy/sell separated
and the largest single print of each side retained, so "big" can be redefined at any
threshold from the same extract, and the whale question can be asked where it is actually
answerable.

GAP 2 -- RESOLUTION. Everything before was 1-minute buckets inside a 15-minute bar. If the
aggressor lifts the last offer and simply stops, exhaustion resolves in seconds. Buckets
are 30s here, so the final 30-60s of the spike is visible for the first time.

Features, all inside [t, t+15m) and known at the decision moment, all signed so POSITIVE
means flow still running in the breakout direction (continuation, the fade's enemy):

  ofi_30 / ofi_60 / ofi_120   terminal OFI over the last 30s / 60s / 2m
  whale_term                  signed flow in prints >= $1k over the last 2m, which is the
                              low-threshold version of the feature that had no coverage
  whale_share_term            share of terminal notional coming from >= $1k prints
  maxprint_term               signed largest single print in the last 2m, scaled by bar
                              notional -- one whale versus many mediums
  stop_ratio                  notional in the last 30s over the bar's mean 30s. The literal
                              "did the aggressor stop" measure, only visible at this
                              resolution
  reversal_sub                fraction of the last 4 sub-buckets whose OFI opposes the
                              breakout -- persistence of the flip rather than one blip

Judged the way pierce was: tercile spread, both halves, day counts, and whether a tilt
lifts bps/trade AND return/|DD| at equal average capital -- checked on the gated set AND
on the superset containing it, because that superset check is what killed the last batch.

  python3 analysis/spike_flow.py spike_events.csv spike_tape.csv.gz
"""
import math, sys
import numpy as np
import pandas as pd

EV = sys.argv[1] if len(sys.argv) > 1 else "spike_events.csv"
TP = sys.argv[2] if len(sys.argv) > 2 else "spike_tape.csv.gz"
SUB_MS, BAR_MS, NOT = 30000, 900000, 35.0
NSUB = BAR_MS // SUB_MS          # 30

ev = pd.read_csv(EV)
tp = pd.read_csv(TP)
print(f"{len(ev):,} events, {len(tp):,} sub-buckets "
      f"({tp.groupby(['coin','win_ms']).ngroups:,} windows populated)")

tp["buy"] = tp.b0 + tp.b1 + tp.b2 + tp.b3
tp["sell"] = tp.s0 + tp.s1 + tp.s2 + tp.s3
tp["ntl"] = tp.buy + tp.sell
tp["big_buy"] = tp.b1 + tp.b2 + tp.b3          # >= $1k
tp["big_sell"] = tp.s1 + tp.s2 + tp.s3
key = list(zip(tp.coin.values, tp.win_ms.values))
tp["_k"] = pd.factorize(pd.Series(key))[0]

# dense [event, sub] matrices
kmap = {}
for i, (c, w) in enumerate(zip(tp.coin.values, tp.win_ms.values)):
    kmap.setdefault((c, w), None)
order = {k: i for i, k in enumerate(kmap)}
E = len(order)
M = {c: np.zeros((E, NSUB)) for c in
     ("buy", "sell", "ntl", "big_buy", "big_sell", "bmax", "smax", "n")}
ri = np.array([order[(c, w)] for c, w in zip(tp.coin.values, tp.win_ms.values)])
si = np.clip(tp["sub"].values, 0, NSUB - 1)
for c in M:
    np.add.at(M[c], (ri, si), tp[c].values)

rows = []
for (c, w), i in order.items():
    rows.append((c, w, i))
mapdf = pd.DataFrame(rows, columns=["sym", "t", "row"])
ev = ev.merge(mapdf, on=["sym", "t"], how="inner")
print(f"{len(ev):,} events matched to tape windows")
R = ev.row.values
d = ev.dirn.values.astype(float)


def ofi_last(nsub):
    b = M["buy"][R, NSUB-nsub:].sum(1); s = M["sell"][R, NSUB-nsub:].sum(1)
    den = b + s
    return np.where(den > 0, d * (b - s) / np.maximum(den, 1e-9), np.nan)


tot_ntl = M["ntl"][R].sum(1)
last1 = M["ntl"][R, NSUB-1:].sum(1)
bb = M["big_buy"][R, NSUB-4:].sum(1); bs = M["big_sell"][R, NSUB-4:].sum(1)
term_ntl = M["ntl"][R, NSUB-4:].sum(1)
mx = np.maximum(M["bmax"][R, NSUB-4:].max(1), 0)
mn = np.maximum(M["smax"][R, NSUB-4:].max(1), 0)

sub_b = M["buy"][R][:, NSUB-4:]; sub_s = M["sell"][R][:, NSUB-4:]
sub_den = sub_b + sub_s
sub_ofi = np.where(sub_den > 0, d[:, None]*(sub_b-sub_s)/np.maximum(sub_den, 1e-9), np.nan)

ev["ofi_30"] = ofi_last(1)
ev["ofi_60"] = ofi_last(2)
ev["ofi_120"] = ofi_last(4)
ev["whale_term"] = np.where(bb+bs > 0, d*(bb-bs)/np.maximum(bb+bs, 1e-9), np.nan)
ev["whale_share_term"] = np.where(term_ntl > 0, (bb+bs)/np.maximum(term_ntl, 1e-9), np.nan)
ev["maxprint_term"] = np.where(tot_ntl > 0, d*(mx-mn)/np.maximum(tot_ntl, 1e-9), np.nan)
mean_sub = tot_ntl / np.maximum((M["n"][R] > 0).sum(1), 1)
ev["stop_ratio"] = np.where(mean_sub > 0, last1/np.maximum(mean_sub, 1e-9), np.nan)
ev["reversal_sub"] = np.nanmean(np.where(sub_ofi < 0, 1.0, 0.0), axis=1)

FEATS = ["ofi_30", "ofi_60", "ofi_120", "whale_term", "whale_share_term",
         "maxprint_term", "stop_ratio", "reversal_sub"]
print("coverage: " + ", ".join(f"{f} {int(ev[f].notna().sum()):,}" for f in FEATS))
print(f"  (gap 1 check — whale_term usable on {int(ev[ev.signalled==1].whale_term.notna().sum()):,} "
      f"gated events, was 255 at the $10k threshold)")
ev["day"] = pd.to_datetime(ev.t, unit="ms").dt.strftime("%m-%d")
ev["half"] = np.where(ev.t <= ev.t.median(), "1st", "2nd")


def block(lab, pop):
    print(f"\n{'='*78}\n### {lab} — n={len(pop):,}, baseline {pop.fade_bps.mean():+.1f} bps\n{'='*78}")
    print(f"  {'feature':>17} {'n':>5} {'LOW':>8} {'MID':>8} {'HIGH':>8} {'L-H':>8} "
          f"{'1st':>7} {'2nd':>7} {'days+':>7}")
    for f in FEATS:
        s = pop[pop[f].notna()].copy()
        if len(s) < 200 or s[f].nunique() < 10:
            continue
        try:
            s["q"] = pd.qcut(s[f], 3, labels=["LOW", "MID", "HIGH"], duplicates="drop")
        except ValueError:
            continue
        m = s.groupby("q", observed=True).fade_bps.mean()
        if "HIGH" not in m.index or "LOW" not in m.index:
            continue
        sp = m["LOW"] - m["HIGH"]
        hs = []
        for h in ("1st", "2nd"):
            sub = s[s.half == h]
            mm = sub.groupby("q", observed=True).fade_bps.mean() if len(sub) > 80 else None
            hs.append(mm["LOW"]-mm["HIGH"] if mm is not None and "HIGH" in mm.index else np.nan)
        byday = s.groupby("day").apply(
            lambda g: (g[g.q == "LOW"].fade_bps.mean()-g[g.q == "HIGH"].fade_bps.mean())
            if (g.q == "LOW").sum() >= 3 and (g.q == "HIGH").sum() >= 3 else np.nan,
            include_groups=False).dropna()
        dstr = f"{int((byday>0).sum())}/{len(byday)}" if len(byday) >= 8 else "-"
        print(f"  {f:>17} {len(s):>5} {m['LOW']:>+8.1f} {m['MID']:>+8.1f} {m['HIGH']:>+8.1f} "
              f"{sp:>+8.1f} {hs[0]:>+7.1f} {hs[1]:>+7.1f} {dstr:>7}")


block("SIGNALLED (gated)", ev[ev.signalled == 1])
block("ALL SPIKES", ev)

print(f"\n{'='*78}\n### TILT PRICING — gated vs the superset containing it\n{'='*78}")
print(f"  {'feature':>17} {'population':>18} {'n':>5} {'bps/trade':>10} {'vs flat':>9} "
      f"{'ret/DD':>8} {'vs flat':>9}")
for f in FEATS:
    for lab, pop in (("gated", ev[ev.signalled == 1]), ("all spikes", ev)):
        s = pop[pop[f].notna()].copy()
        if len(s) < 200 or s[f].nunique() < 10:
            continue
        rk = 1.0 - s[f].rank(pct=True)          # low feature = flow stopped = bet more
        mlt = np.clip(rk/0.5, 0.5, 2.0); mlt = mlt/mlt.mean()
        u = NOT*mlt*s.fade_bps/1e4; uf = NOT*s.fade_bps/1e4
        def rdd(x):
            c = np.cumsum(x.values); dd = float(np.min(c-np.maximum.accumulate(c)))
            return x.sum()/abs(dd) if dd < 0 else np.nan
        print(f"  {f:>17} {lab:>18} {len(s):>5} {float((s.fade_bps*mlt).mean()):>+10.1f} "
              f"{float((s.fade_bps*mlt).mean())-s.fade_bps.mean():>+9.1f} "
              f"{rdd(u):>8.2f} {rdd(u)-rdd(uf):>+9.2f}")
print("\n  A tilt must lift BOTH columns on BOTH populations. The last batch passed on the")
print("  gated 727 and reversed on the 4,419 containing it, which is what an artifact does.")
