#!/usr/bin/env python3
"""The widest live signal: trades entered on a WIDE spread earn far more.

    spread <= 2bps   n=54   -11.1 bps   $ -1.22
    spread  2-5bps   n=61   +20.6 bps   $ +5.53
    spread  > 5bps   n=55   +75.1 bps   $+13.63   t=+2.0, top-3 share -2%

That last row is unusual for this dataset: a positive result that is NOT three trades.
Almost every other cut here has collapsed once concentration was checked. So this one
gets the full treatment before anything is changed.

Three confounds have to be separated, because the bot's own policy entangles them:

  1. EXECUTION. The bot crosses when spread <= 5bps and rests otherwise. So "wide spread"
     and "rested as maker" are nearly the same trades. Resting earns the half-spread; a
     wide spread makes that concession bigger. That is a real edge but a mechanical one,
     and it is bounded by the spread itself -- it cannot explain 86bps.
  2. SELECTION. Wide spreads mark thin coins, which move further, which is what a fade
     needs. Then the finding is really about coin liquidity, and 'tier' already proxies it.
  3. LUCK. n=55 over 10 days.

The decomposition that matters: half the spread is a hard upper bound on how much the
execution channel can contribute. Anything beyond that is coming from trade selection.

  python3 analysis/live_spread.py [trades.csv]
"""
import csv, math, sys
from collections import defaultdict
from datetime import datetime

PATH = sys.argv[1] if len(sys.argv) > 1 else "live_15m_ats/trades_15m.csv"

rows = []
for r in csv.DictReader(open(PATH)):
    try:
        net = float(r["net_bps"])
        sp = (r.get("spread_bps") or "").strip()
        if abs(net) < 1e-9 or not sp:
            continue
        d = dict(sym=r["symbol"], side=r["side"], net=net, gross=float(r["gross_bps"]),
                 fee=float(r["fee_bps"]), pnl=float(r["pnl_usd"]), reason=r["reason"],
                 hold=float(r["hold_h"]), sp=float(sp),
                 crossed=int(float(r["crossed"] or 0)),
                 tier=(r.get("tier") or "").strip(),
                 wait=float(r["entry_wait_s"] or 0),
                 t=datetime.strptime(r["entry_time"], "%Y-%m-%d %H:%M:%S"))
        d["ntl"] = abs(d["pnl"] / (net / 1e4))
        rows.append(d)
    except Exception:
        pass
rows.sort(key=lambda r: r["t"])
N = len(rows)
print(f"{N} trades carry a logged spread (of the full book)\n")


def st(v):
    n = len(v)
    if n < 2:
        return (float("nan"), float("nan"), n)
    mu = sum(v) / n
    sd = (sum((x - mu) ** 2 for x in v) / (n - 1)) ** 0.5
    return (mu, mu / (sd / math.sqrt(n)) if sd > 0 else float("nan"), n)


def conc(seg):
    tot = sum(r["pnl"] for r in seg)
    if abs(tot) < 1e-9:
        return float("nan")
    return 100 * sum(r["pnl"] for r in sorted(seg, key=lambda r: -abs(r["pnl"]))[:3]) / tot


def line(lab, seg, w=24):
    if len(seg) < 3:
        print(f"  {lab:>{w}} n={len(seg):<4} --")
        return
    mu, t, n = st([r["net"] for r in seg])
    print(f"  {lab:>{w}} n={n:<4} {mu:>+8.1f} bps  t={t:>+5.1f}  "
          f"win {100*sum(1 for r in seg if r['net']>0)/n:>3.0f}%  "
          f"${sum(r['pnl'] for r in seg):>+7.2f}  top3 {conc(seg):>+6.0f}%")


WIDE = [r for r in rows if r["sp"] > 5]
TIGHT = [r for r in rows if r["sp"] <= 5]

# ---------- 1. is it execution, or is it selection? ----------
print("=== 1. EXECUTION vs SELECTION ===")
print("  half the spread is the MOST the maker concession can be worth:")
for lab, seg in (("wide (>5bps)", WIDE), ("tight (<=5bps)", TIGHT)):
    mu_sp = sum(r["sp"] for r in seg) / len(seg)
    mu_n, _, _ = st([r["net"] for r in seg])
    mu_g, _, _ = st([r["gross"] for r in seg])
    rested = sum(1 for r in seg if not r["crossed"])
    print(f"  {lab:>16}  mean spread {mu_sp:>5.1f}bps -> half-spread {mu_sp/2:>4.1f}bps max"
          f"   | gross {mu_g:>+7.1f}  net {mu_n:>+7.1f}  rested {rested}/{len(seg)}")
d_net = st([r["net"] for r in WIDE])[0] - st([r["net"] for r in TIGHT])[0]
d_half = (sum(r["sp"] for r in WIDE)/len(WIDE) - sum(r["sp"] for r in TIGHT)/len(TIGHT)) / 2
print(f"\n  wide minus tight  = {d_net:+.1f} bps")
print(f"  half-spread differential (execution ceiling) = {d_half:+.1f} bps")
print(f"  UNEXPLAINED by execution = {d_net - d_half:+.1f} bps  "
      f"({100*(d_net-d_half)/d_net:.0f}% of the gap)")
print("  If most of the gap survives, the spread is selecting better TRADES, not better fills.\n")

# ---------- 2. the clean within-policy test ----------
print("=== 2. DISENTANGLING: rested trades at a TIGHT spread, if any exist ===")
cells = defaultdict(list)
for r in rows:
    cells[(r["sp"] > 5, bool(r["crossed"]))].append(r)
for (wide, cx), seg in sorted(cells.items()):
    line(f"{'wide' if wide else 'tight'} / {'crossed' if cx else 'rested'}", seg)
print("  If the two policies only ever co-occur one way, spread and execution cannot be")
print("  separated from live data alone and the effect must be read as the pair.\n")

# ---------- 3. is it just the tier / thin-coin effect? ----------
print("=== 3. IS IT JUST THIN COINS? spread within each tier ===")
for tn in ("HIGH", "MID", ""):
    seg = [r for r in rows if r["tier"] == tn]
    if len(seg) < 10:
        continue
    print(f"  --- tier {tn or '(unlogged)'} (n={len(seg)}, "
          f"median spread {sorted(r['sp'] for r in seg)[len(seg)//2]:.1f}bps) ---")
    line("wide  >5bps", [r for r in seg if r["sp"] > 5])
    line("tight <=5bps", [r for r in seg if r["sp"] <= 5])
print("  A spread effect that survives INSIDE a tier is not merely a liquidity proxy.\n")

# ---------- 4. concentration, the test that has killed everything else ----------
print("=== 4. CONCENTRATION: is the wide-spread edge one day, one coin, one week? ===")
byday = defaultdict(list)
for r in WIDE:
    byday[r["t"].date()].append(r)
tot = sum(r["pnl"] for r in WIDE)
print(f"  wide-spread total ${tot:+.2f} across {len(byday)} days:")
for d in sorted(byday):
    p = sum(x["pnl"] for x in byday[d])
    print(f"    {d}  n={len(byday[d]):>2}  ${p:>+6.2f}  ({100*p/tot:>+5.0f}% of total)")
best = max(byday, key=lambda d: sum(x["pnl"] for x in byday[d]))
rest = [r for r in WIDE if r["t"].date() != best]
print(f"\n  excluding the single best day ({best}):")
line("wide, ex-best-day", rest)
bycoin = defaultdict(list)
for r in WIDE:
    bycoin[r["sym"]].append(r)
top = sorted(bycoin, key=lambda s: -sum(x["pnl"] for x in bycoin[s]))[:3]
print(f"  top 3 coins by $: " + ", ".join(
    f"{s} ${sum(x['pnl'] for x in bycoin[s]):+.2f} (n={len(bycoin[s])})" for s in top))
line("wide, ex-top-3-coins", [r for r in WIDE if r["sym"] not in top])
half = rows[N // 2]["t"]
line("wide, first half", [r for r in WIDE if r["t"] <= half])
line("wide, second half", [r for r in WIDE if r["t"] > half])
print()

# ---------- 5. does it survive the reclaim/backstop split? ----------
print("=== 5. MECHANISM: does a wide spread change the RECLAIM RATE, or just the size? ===")
for lab, seg in (("wide (>5bps)", WIDE), ("tight (<=5bps)", TIGHT)):
    nb = sum(1 for r in seg if r["reason"].startswith("backstop"))
    rc = [r for r in seg if r["reason"] == "reclaim"]
    bs = [r for r in seg if r["reason"].startswith("backstop")]
    print(f"  {lab:>16}  backstop rate {100*nb/len(seg):>4.0f}%  "
          f"| reclaim {st([r['net'] for r in rc])[0]:>+7.1f}bps (n={len(rc)})  "
          f"| backstop {st([r['net'] for r in bs])[0] if bs else float('nan'):>+8.1f}bps (n={len(bs)})")
print("  A lower BACKSTOP RATE would mean wide spreads pick trades that revert more often.")
print("  A bigger reclaim SIZE would mean they pick the same trades, just further from fair.\n")

# ---------- 6. finer grid, to see if a threshold is even identifiable ----------
print("=== 6. FINER GRID: is there a threshold, or just a monotone drift? ===")
qs = sorted(r["sp"] for r in rows)
edges = [qs[int(len(qs) * f)] for f in (0.2, 0.4, 0.6, 0.8)]
print(f"  quintile edges: {', '.join(f'{e:.1f}' for e in edges)} bps")
prev = -1.0
for i, e in enumerate(edges + [1e9]):
    seg = [r for r in rows if prev < r["sp"] <= e]
    line(f"Q{i+1}  {prev if prev>0 else 0:.1f}-{e if e<1e9 else 999:.1f}bps", seg)
    prev = e
print("\n  A clean monotone rise supports spread as a real feature. A single hot bucket")
print("  with noise either side is the shape that has failed every previous holdout here.")
