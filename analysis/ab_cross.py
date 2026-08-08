#!/usr/bin/env python3
"""The randomised cross-vs-rest experiment: read it out.

Wide-spread entries earn +93.2bps against +3.6bps for tight ones (n=78 vs 152, t=+3.1),
and that survived a clean 60-trade holdout after being wrongly rejected once
(live_spread_holdout.py). Two explanations fit equally well:

  the COINS   a wide spread marks a thin book, thin books overshoot further on a volume
              spike, and a bigger overshoot means a bigger snap back
  the WAITING resting instead of paying up fills you only against someone impatient
              enough to cross the whole spread -- a better price, and plausibly a better
              MOMENT, since that is what peak panic looks like

Observational data cannot separate them, because all 78 wide trades rested and 141 of
152 tight trades crossed. The bot now randomises the crossing decision on tight-spread
signals only (--ab-rest-pct), which breaks the confound by construction.

THE METRIC IS P&L PER SIGNAL, NOT PER FILL. Resting's entire cost is that it fills less
often, so comparing only filled trades would build the answer into the question. A rest
arm that earns +80bps on half as many fills is worth the same as a cross arm earning
+40bps on all of them.

  python3 analysis/ab_cross.py [trades.csv] [misses.csv]
"""
import csv, math, sys
from collections import defaultdict

TR = sys.argv[1] if len(sys.argv) > 1 else "live_15m_ats/trades_15m.csv"
MS = sys.argv[2] if len(sys.argv) > 2 else "live_15m_ats/missed_15m_ats.csv"

fills, misses = defaultdict(list), defaultdict(int)
for r in csv.DictReader(open(TR)):
    a = (r.get("ab_arm") or "").strip()
    try:
        net = float(r["net_bps"])
    except Exception:
        continue
    if a and abs(net) > 1e-9:
        fills[a].append(dict(net=net, pnl=float(r["pnl_usd"]), sym=r["symbol"],
                             reason=r["reason"], sp=float(r["spread_bps"] or 0)))
try:
    for r in csv.DictReader(open(MS)):
        a = (r.get("ab_arm") or "").strip()
        if a:
            misses[a] += 1
except FileNotFoundError:
    pass

if not fills:
    print("No arm-tagged trades yet. The experiment starts logging once the bot runs")
    print("with --ab-rest-pct > 0; every row before that has an empty ab_arm.")
    sys.exit(0)


def st(v):
    n = len(v)
    if n < 2:
        return (float("nan"), float("nan"), float("nan"), n)
    mu = sum(v) / n
    sd = (sum((x - mu) ** 2 for x in v) / (n - 1)) ** 0.5
    return (mu, sd, mu / (sd / math.sqrt(n)) if sd > 0 else float("nan"), n)


print("=== the randomised comparison (tight spreads only) ===")
print(f"  {'arm':>7} {'signals':>8} {'fills':>6} {'fill%':>6} {'bps|fill':>9} "
      f"{'t':>6} {'bps/SIGNAL':>11} {'$ total':>9}")
res = {}
for a in ("cross", "rest"):
    f = fills.get(a, [])
    sig = len(f) + misses.get(a, 0)
    if not sig:
        continue
    mu, sd, t, n = st([x["net"] for x in f])
    fr = len(f) / sig
    per_sig = mu * fr if n else float("nan")
    res[a] = dict(f=f, sig=sig, fr=fr, mu=mu, sd=sd, per_sig=per_sig)
    print(f"  {a:>7} {sig:>8} {len(f):>6} {100*fr:>5.0f}% {mu:>+9.1f} {t:>+6.1f} "
          f"{per_sig:>+11.1f} {sum(x['pnl'] for x in f):>+9.2f}")

if "cross" in res and "rest" in res:
    c, r = res["cross"], res["rest"]
    d = r["per_sig"] - c["per_sig"]
    # se of a difference of means, inflated by the fill-rate scaling on each arm
    se = math.sqrt((c["fr"] * c["sd"]) ** 2 / max(1, len(c["f"]))
                   + (r["fr"] * r["sd"]) ** 2 / max(1, len(r["f"])))
    t = d / se if se > 0 else float("nan")
    print(f"\n  REST minus CROSS: {d:+.1f} bps per signal   t={t:+.2f}   "
          f"(95% CI {d-1.96*se:+.1f} to {d+1.96*se:+.1f})")
    if abs(t) < 2:
        need = int(((1.96 + 0.84) * se / max(abs(d), 1e-9)) ** 2
                   * (len(c["f"]) + len(r["f"])))
        print(f"  Not conclusive. At this effect size, ~{need:,} arm-tagged fills would")
        print(f"  be needed for 80% power; there are {len(c['f'])+len(r['f'])}.")
    else:
        print(f"  Conclusive at this sample: "
              f"{'RESTING wins -- the waiting is what matters' if d > 0 else 'CROSSING wins -- the spread was selecting COINS, not fills'}")

print("\n=== the observational wide arm, for reference only ===")
print("  NOT randomised: these are the trades the bot rests on because the spread is")
print("  wide. They carry the original confound and cannot settle anything.")
w = fills.get("wide", [])
if w:
    mu, sd, t, n = st([x["net"] for x in w])
    sig = n + misses.get("wide", 0)
    print(f"  {'wide':>7} {sig:>8} {n:>6} {100*n/sig:>5.0f}% {mu:>+9.1f} {t:>+6.1f} "
          f"{mu*n/sig:>+11.1f} {sum(x['pnl'] for x in w):>+9.2f}")

print("\n=== what each arm does differently, as a check that the arms are real ===")
for a in ("cross", "rest", "wide"):
    f = fills.get(a, [])
    if len(f) < 3:
        continue
    nb = sum(1 for x in f if x["reason"].startswith("backstop"))
    print(f"  {a:>7}  mean spread {sum(x['sp'] for x in f)/len(f):>5.1f}bps  "
          f"backstop {100*nb/len(f):>3.0f}%  {len({x['sym'] for x in f})} distinct coins")
print("\n  cross and rest should have nearly IDENTICAL mean spreads -- they are the same")
print("  population split by a coin flip. If they differ, the randomisation is broken.")
