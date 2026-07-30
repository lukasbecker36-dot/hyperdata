#!/usr/bin/env python3
"""Can the sizing question actually be settled, and how many trades would it take?

Earlier I estimated ~2,600 trades by treating flat-vs-ats as two independent means with a
257bps standard deviation. That is the wrong test. The rules trade IDENTICAL trades and
differ only in weight, so it is a PAIRED comparison: what matters is the variance of the
per-trade difference, not of either series. Paired tests are far more powerful when the
series are highly correlated, which here they are by construction.

Three things measured, all on real trades:

1. PAIRED difference tests. For each trade, (rule P&L - flat P&L), then a t-test and a
   bootstrap CI on the mean difference. This is the correct test of "does this rule beat
   flat", and it needs far fewer trades than the naive estimate.

2. The CLEANER question. A sizing rule based on ats_ratio can only help if ats_ratio
   predicts trade quality. That is a correlation test on net_bps, which does not involve
   P&L at all and is not confounded by the weighting. If the correlation is indistinguish-
   able from zero, then weighting by it is equivalent to random weighting, and flat wins by
   Occam -- no need to compare P&L at all.

3. REQUIRED N, computed from the observed effect sizes rather than assumed ones.

On Monte Carlo: simulating returns from assumed parameters cannot settle an empirical
question, it only propagates the assumptions. The bootstrap below resamples the REAL
trades, which is the version that carries information.

  python3 analysis/sizing_power.py [trades.csv ...]
"""
import csv, math, random, sys

# "path" or "path:base_notional". The base MATTERS when pooling arms: the multiplier is
# recovered as notional/base, so using one base for arms that ran different ones (live is
# $25, the paper arms are $100) scales their multipliers by 4x and silently inflates the
# ats weighting. Got this wrong once; the correlation in section 2 is scale-invariant and
# was unaffected, but the paired P&L test in section 1 was not.
PATHS = sys.argv[1:] or ["live_15m_ats/trades_15m.csv:25"]
BASE = 25.0
SIZE_REF, SIZE_MIN, SIZE_MAX = 2.0, 0.5, 3.0
NBOOT = 20000
random.seed(11)


def clamp(x):
    return min(SIZE_MAX, max(SIZE_MIN, x))


def load(path, base):
    out = []
    for r in csv.DictReader(open(path)):
        try:
            net = float(r["net_bps"]); pnl = float(r["pnl_usd"])
            if abs(net) < 1e-9:
                continue
            mult = (pnl / (net / 1e4)) / base
            out.append(dict(net=net, mult=mult, sym=r["symbol"]))
        except Exception:
            pass
    return out


def st(v):
    n = len(v)
    if n < 2:
        return (float("nan"), float("nan"), float("nan"), n)
    m = sum(v) / n
    sd = (sum((x - m) ** 2 for x in v) / (n - 1)) ** 0.5
    return (m, sd, (m / (sd / math.sqrt(n)) if sd > 0 else float("nan")), n)


def boot_ci(v, f=lambda s: sum(s) / len(s), lo=2.5, hi=97.5):
    n = len(v)
    outs = []
    for _ in range(NBOOT):
        outs.append(f([v[random.randrange(n)] for _ in range(n)]))
    outs.sort()
    return (outs[int(lo / 100 * NBOOT)], outs[int(hi / 100 * NBOOT)])


rows = []
for spec in PATHS:
    p, _, b = spec.partition(":")
    base = float(b) if b else BASE
    try:
        r = load(p, base)
        rows += r
        print(f"loaded {len(r):>4} trades from {p} (base ${base:.0f}, "
              f"mult {min(e['mult'] for e in r):.2f}-{max(e['mult'] for e in r):.2f})")
    except Exception as e:
        print(f"skip {p}: {e}")
if len(rows) < 20:
    print("too few trades"); sys.exit(0)
n = len(rows)
print(f"total {n} trades\n")

RULES = {"ats": lambda e: e["mult"],
         "inverse": lambda e: clamp(1.0 / e["mult"]) if e["mult"] > 0 else 1.0}

print("=== 1. PAIRED test: (rule - flat) per trade, in dollars at $%.0f base ===" % BASE)
print(f"  {'rule vs flat':>14} {'mean diff':>11} {'sd of diff':>11} {'t':>7} "
      f"{'bootstrap 95% CI':>24}")
for name, f in RULES.items():
    d = [(f(e) - 1.0) * BASE * e["net"] / 1e4 for e in rows]
    m, sd, t, _ = st(d)
    lo, hi = boot_ci(d)
    verdict = "beats flat" if lo > 0 else ("loses to flat" if hi < 0 else "indistinguishable")
    print(f"  {name:>14} {m:>+11.4f} {sd:>11.3f} {t:>+7.2f} "
          f"  [{lo:+.3f}, {hi:+.3f}]  {verdict}")
print("  (paired, so this is far more powerful than comparing the two totals separately)")

print("\n=== 2. The cleaner question: does ats_ratio predict trade quality at all? ===")
xs = [e["mult"] for e in rows]
ys = [e["net"] for e in rows]
mx, my = sum(xs) / n, sum(ys) / n
cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / (n - 1)
sx = (sum((a - mx) ** 2 for a in xs) / (n - 1)) ** 0.5
sy = (sum((b - my) ** 2 for b in ys) / (n - 1)) ** 0.5
r = cov / (sx * sy) if sx > 0 and sy > 0 else float("nan")
tr = r * math.sqrt((n - 2) / max(1e-12, 1 - r * r))
pairs = list(zip(xs, ys))


def corr(sample):
    a = [p[0] for p in sample]; b = [p[1] for p in sample]
    m1, m2 = sum(a) / len(a), sum(b) / len(b)
    c = sum((u - m1) * (v - m2) for u, v in sample)
    d1 = math.sqrt(sum((u - m1) ** 2 for u in a)); d2 = math.sqrt(sum((v - m2) ** 2 for v in b))
    return c / (d1 * d2) if d1 > 0 and d2 > 0 else 0.0


lo, hi = boot_ci(pairs, corr)
print(f"  corr(ats multiplier, net_bps) = {r:+.3f}   t={tr:+.2f}   "
      f"bootstrap 95% CI [{lo:+.3f}, {hi:+.3f}]")
print("  If this CI straddles 0, the multiplier carries no information about trade")
print("  quality, and weighting by it is equivalent to weighting at random -- in which")
print("  case FLAT wins by default, because it adds no variance for no expected return.")

print("\n=== 3. Trades needed, from the OBSERVED effect sizes ===")
print(f"  {'comparison':>24} {'observed':>10} {'sd':>9} {'n for t=2':>11} {'days @16/day':>13}")
for name, f in RULES.items():
    d = [(f(e) - 1.0) * BASE * e["net"] / 1e4 for e in rows]
    m, sd, _, _ = st(d)
    need = (2 * sd / abs(m)) ** 2 if m != 0 else float("inf")
    print(f"  {name + ' vs flat (paired)':>24} {m:>+10.4f} {sd:>9.3f} {need:>11,.0f} "
          f"{need/16:>13,.0f}")
naive_sd = st([e["net"] for e in rows])[1]
naive_m = st([e["net"] for e in rows])[0]
print(f"  {'the edge itself':>24} {naive_m:>+10.1f} {naive_sd:>9.1f} "
      f"{(2*naive_sd/abs(naive_m))**2:>11,.0f} {(2*naive_sd/abs(naive_m))**2/16:>13,.0f}")
print()
print("Monte Carlo with assumed return distributions would not help: it can only restate")
print("the assumptions fed into it. The bootstrap above resamples the actual trades, which")
print("is the only simulation here that carries new information.")
