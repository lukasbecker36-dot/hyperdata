#!/usr/bin/env python3
"""Does ats sizing beat flat, or does inverse-ats beat both? Settled on real fills.

The claim: a volume spike driven by LARGE individual trades ("whales") fades harder, so
size up on it. The bot's rule is mult = clamp(ats_ratio / 2, 0.5, 3.0), where ats_ratio
is (bar volume / trade count) against its trailing median.

The whole question reduces to one quantity. Sizing cannot change whether a trade wins --
it only levers what the trade was going to do anyway:

    P&L = base * SUM(mult_i * bps_i)  =  base * N * [ mean(mult)*mean(bps) + cov(mult,bps) ]

The first term is just "bet more", which any rule can buy by scaling up and which is not
an edge. ONLY cov(mult, bps) is skill. So every rule here is normalised to mean(mult)=1,
which holds average deployed capital equal across rules and isolates the covariance. An
earlier version of this analysis skipped that step and produced a spurious "ats beats
flat, t=+2.61" purely because ats deploys ~20% more capital.

Two independent samples:
  LIVE   flat-sized at $35 with ats_ratio logged per trade. Because every trade is the
         same size, ats_ratio CANNOT have influenced the outcome -- this is the clean
         test of whether the signal carries information at all.
  PAPER  actually ats-sized, so the multiplier is recovered as notional/base.

  python3 analysis/ats_verdict.py [live.csv] [paper.csv]
"""
import csv, math, random, sys
from collections import defaultdict
from datetime import datetime

LIVE = sys.argv[1] if len(sys.argv) > 1 else "live_15m_ats/trades_15m.csv"
PAPER = sys.argv[2] if len(sys.argv) > 2 else "paper_15m_ats/trades_15m.csv"
SIZE_REF, SIZE_MIN, SIZE_MAX = 2.0, 0.5, 3.0
random.seed(7)


def load(path, base, ats_col=True):
    out = []
    for r in csv.DictReader(open(path)):
        try:
            net = float(r["net_bps"])
            if abs(net) < 1e-9:
                continue
            pnl = float(r["pnl_usd"])
            ntl = abs(pnl / (net / 1e4))
            a = None
            if ats_col:
                v = (r.get("ats_ratio") or "").strip()
                a = float(v) if v else None
            else:
                # paper is ats-sized: recover ats from the multiplier it applied
                mult = ntl / base
                if SIZE_MIN + 1e-6 < mult < SIZE_MAX - 1e-6:
                    a = mult * SIZE_REF
            out.append(dict(sym=r["symbol"], net=net, ats=a, ntl=ntl,
                            reason=r["reason"],
                            t=datetime.strptime(r["entry_time"], "%Y-%m-%d %H:%M:%S")))
        except Exception:
            pass
    return sorted(out, key=lambda x: x["t"])


def m_ats(a):
    return min(SIZE_MAX, max(SIZE_MIN, a / SIZE_REF))


def m_inv(a):
    return min(SIZE_MAX, max(SIZE_MIN, SIZE_REF / a)) if a else 1.0


def norm(ms):
    """scale multipliers to mean 1 -- equal average capital across rules"""
    mu = sum(ms) / len(ms)
    return [m / mu for m in ms]


def st(v):
    n = len(v)
    if n < 2:
        return (float("nan"), float("nan"), n)
    mu = sum(v) / n
    sd = (sum((x - mu) ** 2 for x in v) / (n - 1)) ** 0.5
    return (mu, mu / (sd / math.sqrt(n)) if sd > 0 else float("nan"), n)


def report(label, rows, base):
    seg = [r for r in rows if r["ats"] is not None]
    if len(seg) < 20:
        print(f"\n### {label}: only {len(seg)} trades carry ats — skipping\n")
        return
    bps = [r["net"] for r in seg]
    n = len(seg)
    print(f"\n### {label} — {n} trades with a usable ats_ratio "
          f"(of {len(rows)}), base ${base:.0f}")
    mu, t, _ = st(bps)
    print(f"    baseline: {mu:+.1f} bps/trade, t={t:+.2f}, "
          f"ats_ratio median {sorted(r['ats'] for r in seg)[n//2]:.2f}")

    # --- the only thing that matters: does the multiplier co-vary with the outcome? ---
    print("\n    correlation of each rule's multiplier with net_bps"
          "  (this IS the edge; everything else is leverage)")
    for name, fn in (("ats  (size UP on big trades)", m_ats),
                     ("inv  (size DOWN on big trades)", m_inv)):
        ms = [fn(r["ats"]) for r in seg]
        mm, bm = sum(ms) / n, sum(bps) / n
        sm = math.sqrt(sum((x - mm) ** 2 for x in ms) / (n - 1))
        sb = math.sqrt(sum((x - bm) ** 2 for x in bps) / (n - 1))
        cov = sum((ms[i] - mm) * (bps[i] - bm) for i in range(n)) / (n - 1)
        rho = cov / (sm * sb) if sm > 0 and sb > 0 else float("nan")
        tr = rho * math.sqrt((n - 2) / max(1e-12, 1 - rho * rho))
        lo, hi = rho - 1.96 / math.sqrt(n - 3), rho + 1.96 / math.sqrt(n - 3)
        print(f"      {name:<32} corr {rho:+.3f}  t={tr:+.2f}  95% CI [{lo:+.3f}, {hi:+.3f}]")

    # --- simulate the rules at EQUAL average capital ---
    print("\n    P&L at equal average deployed capital (multipliers normalised to mean 1)")
    print(f"      {'rule':<10} {'mean mult':>10} {'$ total':>9} {'vs flat':>9} "
          f"{'bps/trade':>10} {'top3':>7}")
    flat_tot = base * sum(bps) / 1e4
    out = {}
    for name, fn in (("flat", lambda a: 1.0), ("ats", m_ats), ("inverse", m_inv)):
        raw = [fn(r["ats"]) for r in seg]
        ms = norm(raw)
        d = [base * ms[i] * bps[i] / 1e4 for i in range(n)]
        tot = sum(d)
        top3 = sorted(d, key=lambda x: -abs(x))[:3]
        out[name] = d
        print(f"      {name:<10} {sum(raw)/n:>10.2f} {tot:>+9.2f} {tot-flat_tot:>+9.2f} "
              f"{sum(ms[i]*bps[i] for i in range(n))/n:>+10.1f} "
              f"{100*sum(top3)/tot if tot else float('nan'):>6.0f}%")

    # --- paired test: same trades, difference in $ per trade ---
    print("\n    paired difference vs flat (same trades, so this is the powerful test)")
    for name in ("ats", "inverse"):
        d = [out[name][i] - out["flat"][i] for i in range(n)]
        mu, t, _ = st(d)
        boot = []
        for _ in range(2000):
            s = [d[random.randrange(n)] for _ in range(n)]
            boot.append(sum(s))
        boot.sort()
        print(f"      {name:<10} {sum(d):+7.2f} total   {mu:+.4f}/trade  t={t:+.2f}   "
              f"95% CI [{boot[50]:+.2f}, {boot[1949]:+.2f}]")

    # --- concentration by month, the test that has killed everything else here ---
    print("\n    ats-minus-flat by week (a result that is one week is not a result)")
    byw = defaultdict(float)
    for i, r in enumerate(seg):
        byw[r["t"].strftime("%Y-W%W")] += out["ats"][i] - out["flat"][i]
    tot = sum(byw.values())
    for w in sorted(byw):
        print(f"      {w}  {byw[w]:+7.2f}  ({100*byw[w]/tot if tot else float('nan'):>+5.0f}% of total)")


live = load(LIVE, 35.0, ats_col=True)
paper = load(PAPER, 100.0, ats_col=False)
print(f"loaded {len(live)} live, {len(paper)} paper")
print("\nNOTE: live is FLAT-sized, so ats_ratio cannot have affected any live outcome.")
print("That makes live the clean read on whether the signal carries information;")
print("paper's ats sizing is entangled with its own results by construction.")
report("LIVE (flat $35, ats logged)", live, 35.0)
report("PAPER 15m-ats (ats-sized $100 base)", paper, 100.0)

print("\n" + "=" * 78)
print("A sizing rule only pays if corr(multiplier, net_bps) > 0. If both ats and inverse")
print("sit on zero, the ats_ratio carries no information about trade quality and the")
print("choice between them is cosmetic -- pick flat, because it has the lowest variance")
print("and does not concentrate risk into the trades with the widest outcomes.")
