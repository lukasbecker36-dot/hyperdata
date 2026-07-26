#!/usr/bin/env python3
"""Cross IMMEDIATELY at signal time, instead of resting then crossing on timeout.

Every earlier test crossed at the TIMEOUT (taker_entry.py, entry_policy.py) and lost,
because by then price had walked 7-35bps away -- the drift, not the fee, was the killer.
Crossing at t0 has no drift at all. You pay only:
    the spread (you take the far side of the book instead of resting on the near side)
    +3bps of fee (taker 4.5 vs maker 1.5)
and in exchange you fill 100% of signals -- including the ~24% that never filled as a
maker, which the audit showed were the BEST trades (+$0.87/trade of unreachable alpha).

So this is the one taker configuration that could plausibly win, and it has not been
tested. It is measured here on the real paper trades using the bid/ask the bot actually
recorded at placement, so the spread is observed rather than assumed.

  MAKER-ABANDON (live bot today) : only trades whose entry printed through, at the resting
                                   price. This is the audited "real" P&L.
  TAKER-NOW                      : every signal, entered at the far side of the book.
                                   short -> sell the BID; long -> buy the ASK.

Exit is priced both ways, since the audit found ~25% of exits do not fill passively either.

  python3 analysis/taker_now.py [report.csv] [arm_glob]
"""
import csv, glob, math, os, sys
from collections import defaultdict

REPORT = sys.argv[1] if len(sys.argv) > 1 else "live/shadow_fill_report_v2.csv"
ARMS = sys.argv[2] if len(sys.argv) > 2 else "live/paper_*.csv"
MAKER_BPS, TAKER_BPS = 1.5, 4.5

# ---- notional + book snapshot per trade, from the arm logs ----
info = {}
for path in sorted(glob.glob(ARMS)):
    for r in csv.DictReader(open(path)):
        try:
            net = float(r["net_bps"]); pnl = float(r["pnl_usd"])
            if abs(net) < 1e-12:
                continue
            key = (r["symbol"], r["side"], r["reason"], round(pnl, 4))
            info[key] = dict(notional=pnl/(net/1e4),
                             e_bid=float(r["entry_bid"]), e_ask=float(r["entry_ask"]),
                             entry=float(r["entry_px"]), exit=float(r["exit_px"]))
        except Exception:
            pass

rows = defaultdict(list)
miss = defaultdict(int)
for r in csv.DictReader(open(REPORT)):
    key = (r["sym"], r["side"], r["reason"], round(float(r["pnl"]), 4))
    d = info.get(key)
    if not d:
        miss[r["arm"]] += 1
        continue
    rows[r["arm"]].append(dict(pnl=float(r["pnl"]), side=r["side"],
                               filled=(r["entry_fill_300"] == "through"), **d))
if not rows:
    print("no joined trades"); sys.exit(0)


def median(xs):
    s = sorted(xs); n = len(s)
    return s[n//2] if n % 2 else 0.5*(s[n//2-1]+s[n//2])


def st(xs):
    n = len(xs)
    if n < 2: return (float('nan'), float('nan'), n)
    m = sum(xs)/n
    sd = (sum((x-m)**2 for x in xs)/(n-1))**0.5
    return (m, m/(sd/math.sqrt(n)) if sd > 0 else float('nan'), n)


def taker_pnl(t, exit_bps):
    """Enter at the far side of the book at t0; exit unchanged."""
    d = 1 if t["side"] == "LONG" else -1
    px = t["e_bid"] if d < 0 else t["e_ask"]      # short sells the bid, long buys the ask
    if px <= 0:
        return None
    gross = d * (t["exit"] - px) / px
    return t["notional"] * (gross - (TAKER_BPS + exit_bps) / 1e4)


print("Crossing at SIGNAL TIME vs resting-and-abandoning")
print("(real recorded bid/ask at placement, so the spread is observed not assumed)\n")
hdr = (f"{'arm':20s} {'n':>4} {'fill%':>6} {'spread':>7} | {'MAKER-ABANDON':>14} | "
       f"{'TAKER-NOW mk':>13} {'TAKER-NOW tk':>13} | {'best':>13}")
print(hdr); print("-" * len(hdr))
T = defaultdict(float)
for arm, ts in sorted(rows.items()):
    sp = []
    for t in ts:
        mid = 0.5*(t["e_bid"] + t["e_ask"])
        if mid > 0:
            sp.append((t["e_ask"] - t["e_bid"]) / mid * 1e4)
    mk_ab = sum(t["pnl"] for t in ts if t["filled"])
    tk_mk = [taker_pnl(t, MAKER_BPS) for t in ts]
    tk_tk = [taker_pnl(t, TAKER_BPS) for t in ts]
    tk_mk = [x for x in tk_mk if x is not None]
    tk_tk = [x for x in tk_tk if x is not None]
    nf = sum(1 for t in ts if t["filled"])
    best = "TAKER-NOW" if sum(tk_tk) > mk_ab else "maker-abandon"
    print(f"{arm:20s} {len(ts):>4} {nf/len(ts)*100:>5.0f}% {median(sp) if sp else 0:>6.1f}b | "
          f"{mk_ab:>+14.2f} | {sum(tk_mk):>+13.2f} {sum(tk_tk):>+13.2f} | {best:>13}")
    T["mk_ab"] += mk_ab; T["tk_mk"] += sum(tk_mk); T["tk_tk"] += sum(tk_tk)
    T["n"] += len(ts); T["nf"] += nf
print("-" * len(hdr))
print(f"{'TOTAL':20s} {int(T['n']):>4} {T['nf']/T['n']*100:>5.0f}% {'':>7} | "
      f"{T['mk_ab']:>+14.2f} | {T['tk_mk']:>+13.2f} {T['tk_tk']:>+13.2f} | "
      f"{'TAKER-NOW' if T['tk_tk'] > T['mk_ab'] else 'maker-abandon':>13}")
print()
print(f"extra trades captured by crossing: {int(T['n'] - T['nf'])} "
      f"({(1 - T['nf']/T['n'])*100:.0f}% of signals the maker version never got)")

# what do the two groups contribute under TAKER-NOW?
allts = [t for ts in rows.values() for t in ts]
for lab, grp in (("would have filled as maker", [t for t in allts if t["filled"]]),
                 ("would NOT have filled", [t for t in allts if not t["filled"]])):
    v = [taker_pnl(t, TAKER_BPS) for t in grp]
    v = [x for x in v if x is not None]
    m, tt, n = st(v)
    print(f"  {lab:>28}: n={n:<4} total ${sum(v):+8.2f}  ${m:+.3f}/trade  t={tt:+.1f}")
print()
print("The second line is the whole case for crossing: those are the signals the maker")
print("version simply never traded. If they are strongly positive even after paying the")
print("spread and the taker fee, immediate crossing beats patience.")
