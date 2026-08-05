#!/usr/bin/env python3
"""What the live fills actually say, after 177 completed trades.

Everything here is descriptive of REAL money on REAL fills -- no backtest, no
reconstruction. That makes it small (n=177) and therefore weak, so every cut is
reported with its concentration: on this dataset headline aggregates have
repeatedly turned out to be three trades. A cut whose top-3 share is near 100%
is a story about three trades, not about the cut.

  python3 analysis/live_review.py [trades.csv]
"""
import csv, math, sys
from collections import defaultdict
from datetime import datetime

PATH = sys.argv[1] if len(sys.argv) > 1 else "live_15m_ats/trades_15m.csv"

rows = []
for r in csv.DictReader(open(PATH)):
    try:
        d = dict(r)
        d["net"] = float(r["net_bps"])
        d["pnl"] = float(r["pnl_usd"])
        d["hold"] = float(r["hold_h"] or 0)
        d["sz"] = float(r["sz"] or 0)
        d["t"] = datetime.strptime(r["entry_time"], "%Y-%m-%d %H:%M:%S")
        d["ct"] = datetime.strptime(r["close_time"], "%Y-%m-%d %H:%M:%S")
        d["notional"] = abs(d["pnl"] / (d["net"] / 1e4)) if abs(d["net"]) > 1e-9 else 0.0
        for k in ("entry_wait_s", "exit_wait_s", "spread_bps", "ats_ratio",
                  "queue_ratio", "vpin30", "adverse_ofi", "repegs", "crossed",
                  "exit_taker", "fee_usd"):
            v = (r.get(k) or "").strip()
            d[k] = float(v) if v else None
        rows.append(d)
    except Exception:
        pass
rows.sort(key=lambda r: r["t"])
N = len(rows)


def st(v):
    n = len(v)
    if n < 2:
        return (float("nan"), float("nan"), n)
    mu = sum(v) / n
    sd = (sum((x - mu) ** 2 for x in v) / (n - 1)) ** 0.5
    return (mu, mu / (sd / math.sqrt(n)) if sd > 0 else float("nan"), n)


def conc(seg):
    """share of total $ P&L from the 3 biggest absolute contributors"""
    tot = sum(r["pnl"] for r in seg)
    if abs(tot) < 1e-9:
        return float("nan")
    top = sorted(seg, key=lambda r: -abs(r["pnl"]))[:3]
    return 100 * sum(r["pnl"] for r in top) / tot


def line(lab, seg, w=22):
    if len(seg) < 3:
        print(f"  {lab:>{w}} n={len(seg):<4} --")
        return
    mu, t, n = st([r["net"] for r in seg])
    tot = sum(r["pnl"] for r in seg)
    win = 100 * sum(1 for r in seg if r["net"] > 0) / n
    print(f"  {lab:>{w}} n={n:<4} {mu:>+8.1f} bps  t={t:>+5.1f}  "
          f"win {win:>3.0f}%  ${tot:>+7.2f}  top3 {conc(seg):>+6.0f}%")


tot = sum(r["pnl"] for r in rows)
mu, t, _ = st([r["net"] for r in rows])
span = (rows[-1]["ct"] - rows[0]["t"]).total_seconds() / 86400
print(f"=== {N} completed trades over {span:.1f} days, ${tot:+.2f} total ===")
print(f"  mean {mu:+.1f} bps  t={t:+.2f}   median {sorted(r['net'] for r in rows)[N//2]:+.1f} bps")
print(f"  win rate {100*sum(1 for r in rows if r['net']>0)/N:.0f}%   "
      f"fees ${sum(r['fee_usd'] or 0 for r in rows):.2f}   "
      f"mean notional ${sum(r['notional'] for r in rows)/N:.2f}")

print("\n=== the distribution: where does the money actually come from? ===")
s = sorted(rows, key=lambda r: r["pnl"])
print(f"  {'worst 5':>22}  " + "  ".join(f"{r['symbol']} {r['pnl']:+.2f}" for r in s[:5]))
print(f"  {'best 5':>22}  " + "  ".join(f"{r['symbol']} {r['pnl']:+.2f}" for r in s[-5:]))
gains = sum(r["pnl"] for r in rows if r["pnl"] > 0)
losses = sum(r["pnl"] for r in rows if r["pnl"] < 0)
print(f"  gross gains ${gains:+.2f} / gross losses ${losses:+.2f}  "
      f"-> profit factor {gains/abs(losses):.2f}")
for k in (1, 3, 5, 10):
    print(f"  P&L excluding the {k:>2} worst trades: ${tot - sum(r['pnl'] for r in s[:k]):>+7.2f}"
          f"   excluding the {k:>2} best: ${tot - sum(r['pnl'] for r in s[-k:]):>+7.2f}")

print("\n=== by exit reason ===")
for rsn in sorted({r["reason"] for r in rows}):
    line(rsn, [r for r in rows if r["reason"] == rsn])

print("\n=== by side ===")
for sd in ("LONG", "SHORT"):
    line(sd, [r for r in rows if r["side"] == sd])

print("\n=== by tier (bot's own contemporaneous label) ===")
for tn in ("HIGH", "MID", "LOW", ""):
    seg = [r for r in rows if (r.get("tier") or "").strip() == tn]
    line(tn or "(unlogged)", seg)

print("\n=== by entry execution ===")
line("crossed the spread", [r for r in rows if r["crossed"] == 1])
line("rested (maker)", [r for r in rows if r["crossed"] == 0])
q = [r for r in rows if r["entry_wait_s"] is not None]
if q:
    q.sort(key=lambda r: r["entry_wait_s"])
    for lab, seg in (("wait < 5s", [r for r in q if r["entry_wait_s"] < 5]),
                     ("wait 5-60s", [r for r in q if 5 <= r["entry_wait_s"] < 60]),
                     ("wait > 60s", [r for r in q if r["entry_wait_s"] >= 60])):
        line(lab, seg)

print("\n=== by hold time ===")
for lab, f in (("< 1h", lambda h: h < 1), ("1-4h", lambda h: 1 <= h < 4),
               ("4-8h", lambda h: 4 <= h < 8), (">= 8h", lambda h: h >= 8)):
    line(lab, [r for r in rows if f(r["hold"])])

print("\n=== by ats_ratio quartile (sizing hypothesis, now decoupled from size) ===")
a = sorted([r for r in rows if r["ats_ratio"] is not None], key=lambda r: r["ats_ratio"])
if len(a) >= 20:
    k = len(a) // 4
    for i, lab in enumerate(("Q1 lowest ats", "Q2", "Q3", "Q4 highest ats")):
        line(lab, a[i*k:(i+1)*k if i < 3 else len(a)])

print("\n=== by spread at entry ===")
sp = [r for r in rows if r["spread_bps"] is not None]
for lab, f in (("<= 2 bps", lambda x: x <= 2), ("2-5 bps", lambda x: 2 < x <= 5),
               ("> 5 bps", lambda x: x > 5)):
    line(lab, [r for r in sp if f(r["spread_bps"])])

print("\n=== per coin, coins with >= 4 trades ===")
by = defaultdict(list)
for r in rows:
    by[r["symbol"]].append(r)
big = sorted((v for v in by.values() if len(v) >= 4), key=lambda v: sum(x["pnl"] for x in v))
for v in big:
    line(v[0]["symbol"], v, w=14)
print(f"  ({len(by)} distinct coins, {sum(1 for v in by.values() if len(v)==1)} traded once)")

print("\n=== equity curve by day ===")
day = defaultdict(list)
for r in rows:
    day[r["ct"].date()].append(r)
run = 0.0
for d in sorted(day):
    p = sum(x["pnl"] for x in day[d])
    run += p
    bar = ("+" if p >= 0 else "-") * min(40, int(abs(p) * 8))
    print(f"  {d}  n={len(day[d]):>3}  ${p:>+7.2f}  cum ${run:>+7.2f}  {bar}")

print("\n=== concurrency: were entries clustered? ===")
opens = []
for r in rows:
    n_open = sum(1 for o in rows if o["t"] <= r["t"] < o["ct"] and o is not r)
    opens.append((n_open, r))
for lab, f in (("alone or 1 other", lambda n: n <= 1), ("2-5 open", lambda n: 2 <= n <= 5),
               ("6+ open", lambda n: n >= 6)):
    line(lab, [r for n, r in opens if f(n)])
print(f"  max concurrent observed: {max(n for n, _ in opens)}")
