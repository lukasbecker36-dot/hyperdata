#!/usr/bin/env python3
"""Does order flow say which breakouts CONTINUE and which REVERT -- in both regimes?

The tempting question after 08-19 is "does momentum work now". It does not: continuation
ran +241.9 / +163.7 / +242.6 bps on 08-19/20/21 and then -585.4 on 08-22, so it was a
three-day episode that has already reversed. Fitting an entry rule to it would be fitting
to the window that just caused the loss.

The durable question is regime-independent: is there a flow signature at the signal bar
that separates breakouts which keep going from ones that come back? If so it should sort
outcomes in BOTH the reversion regime and the continuation regime, with the same sign on
the underlying quantity. A feature that only sorts in one is a regime proxy, which is the
thing that cannot be timed.

Flow is measured strictly inside the signal bar at 30s resolution with prints split by size
band, so nothing can see its own outcome. Every earlier flow batch ran entirely inside the
reversion regime; this is the first time the same features can be checked against a period
where breakouts behaved the opposite way, which makes it a new test rather than a re-run.

  python3 analysis/flow_continuation.py momentum_events.csv mom_tape.csv.gz
"""
import math, sys
import numpy as np
import pandas as pd

EV = sys.argv[1] if len(sys.argv) > 1 else "momentum_events.csv"
TP = sys.argv[2] if len(sys.argv) > 2 else "mom_tape.csv.gz"
CUT, NSUB = "08-19", 30

ev = pd.read_csv(EV)
tp = pd.read_csv(TP)
tp["buy"] = tp.b0 + tp.b1 + tp.b2 + tp.b3
tp["sell"] = tp.s0 + tp.s1 + tp.s2 + tp.s3
tp["ntl"] = tp.buy + tp.sell
tp["bigb"] = tp.b2 + tp.b3
tp["bigs"] = tp.s2 + tp.s3
order = {}
for c, w in zip(tp.coin.values, tp.win_ms.values):
    order.setdefault((c, w), len(order))
E = len(order)
M = {k: np.zeros((E, NSUB)) for k in ("buy", "sell", "ntl", "bigb", "bigs", "n")}
ri = np.array([order[(c, w)] for c, w in zip(tp.coin.values, tp.win_ms.values)])
si = np.clip(tp["sub"].values, 0, NSUB - 1)
for k in M:
    np.add.at(M[k], (ri, si), tp[k].values)
mp = pd.DataFrame([(c, w, i) for (c, w), i in order.items()], columns=["sym", "t", "row"])
ev = ev.merge(mp, on=["sym", "t"], how="inner")
R = ev.row.values
d = ev.dirn.values.astype(float)
print(f"{len(ev):,} events matched to tape windows")


def ofi(nsub):
    b, s = M["buy"][R, NSUB-nsub:].sum(1), M["sell"][R, NSUB-nsub:].sum(1)
    den = b + s
    return np.where(den > 0, d*(b-s)/np.maximum(den, 1e-9), np.nan)


bb = M["bigb"][R, NSUB-4:].sum(1)
bs = M["bigs"][R, NSUB-4:].sum(1)
tot = M["ntl"][R].sum(1)
ev["ofi_60"] = ofi(2)
ev["ofi_120"] = ofi(4)
ev["ofi_bar"] = ofi(NSUB)
ev["whale"] = np.where(bb+bs > 0, d*(bb-bs)/np.maximum(bb+bs, 1e-9), np.nan)
ev["term_n"] = M["n"][R][:, NSUB-4:].sum(1)
ev["late_share"] = np.where(tot > 0, M["ntl"][R, NSUB-6:].sum(1)/np.maximum(tot, 1e-9), np.nan)
FEATS = ["ofi_60", "ofi_120", "ofi_bar", "whale", "late_share"]

pre = ev[ev.day < CUT]
post_ = ev[ev.day >= CUT]
print(f"  reversion regime {len(pre):,}   continuation regime {len(post_):,}\n")


def block(feat, col, minn=0):
    print(f"--- {feat} vs {col}   (term_n >= {minn})")
    print(f"  {'regime':>16} {'n':>6} {'LOW':>9} {'MID':>9} {'HIGH':>9} {'H-L':>9} {'corr':>8}")
    out = {}
    for lab, s in (("reversion", pre), ("continuation", post_)):
        s = s[s[feat].notna() & s[col].notna() & (s.term_n >= minn)].copy()
        if len(s) < 150:
            continue
        try:
            s["q"] = pd.qcut(s[feat], 3, labels=["LOW", "MID", "HIGH"], duplicates="drop")
        except ValueError:
            continue
        m = s.groupby("q", observed=True)[col].mean()
        r = np.corrcoef(s[feat], s[col])[0, 1]
        out[lab] = m.get("HIGH", np.nan) - m.get("LOW", np.nan)
        print(f"  {lab:>16} {len(s):>6} {m.get('LOW', np.nan):>+9.0f} "
              f"{m.get('MID', np.nan):>+9.0f} {m.get('HIGH', np.nan):>+9.0f} "
              f"{out[lab]:>+9.0f} {r:>+8.3f}")
    if len(out) == 2:
        a, b_ = list(out.values())
        print(f"  {'':>16} same sign in both regimes: "
              f"{'YES' if a*b_ > 0 else 'NO - regime proxy, not a selector'}")
    print()


print("=== flow vs CONTINUATION return (+8 bars, net of costs) ===\n")
for f in FEATS:
    block(f, "m8")
print("=== restricted to bars where flow is measurable (term_n >= 30) ===\n")
for f in ("ofi_120", "whale"):
    block(f, "m8", minn=30)
