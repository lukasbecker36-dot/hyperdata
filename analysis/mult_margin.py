#!/usr/bin/env python3
"""Worst-case single-position margin under MULT_MAX, replayed on the live book.

Written 2026-09-04, when the live arm's base notional was scaled $24 -> $44 alongside
a collateral top-up ($387 -> $713). The MULT_MAX comment in paper_bot.py justified the
4.0x cap in DOLLARS ("4.0 x $24 = $96"), so raising the base silently invalidated the
stated reasoning and the arithmetic had to be redone rather than assumed.

The quantity that matters is not the notional but the MARGIN of one isolated position:

    margin = MULT_MAX x notional / leverage

because _lev_for caps leverage per coin for a LIQ_SIGMA cushion and can floor it at 1x,
in which case the whole notional is margin, and an isolated liquidation loses all of it.

Result on 917 live trades: the peak is real, not a tail bound -- 2.5% of trades hit the
4.0x cap, 9.4% run at 1x, and the two HAVE coincided, so the observed worst equals the
theoretical worst exactly. But it is the same risk as before in fraction terms (24.8%
of collateral at $24/$387; 24.7% at $44/$713), so it needed no new cap. The invariant
to hold on any future resize is

    MULT_MAX x notional / collateral  ~=  25%

Raising the notional WITHOUT the collateral is what breaks it.

    python3 analysis/mult_margin.py [trades_15m.csv] [base:collateral ...]
"""
import csv
import math
import sys

# mirrors paper_bot.py -- kept explicit so this script is readable on its own
SIZE_REF, SIZE_MIN, SIZE_MAX = 2.0, 0.5, 3.0
PIERCE_REF, PIERCE_MAX = 0.5, 2.0
MULT_MAX = 4.0
# mirrors live_bot_ats.py
LIQ_SIGMA, BACKSTOP_BARS = 6.0, 32
MAXLEV = 3          # 128 perps cap at 3x, so maintenance ~ 1/(2*3)


def size_mult(ats, pierce_pct):
    """paper_bot.size_mult: ats leg x pierce leg, each clipped, product clipped."""
    m = 1.0
    if ats is not None:
        m *= min(SIZE_MAX, max(SIZE_MIN, ats / SIZE_REF))
    if pierce_pct is not None:
        m *= min(PIERCE_MAX, max(SIZE_MIN, pierce_pct / PIERCE_REF))
    return min(MULT_MAX, max(SIZE_MIN, m))


def lev_for(rv):
    """live_bot_ats._lev_for: highest leverage leaving a LIQ_SIGMA cushion over the hold."""
    if not rv or rv <= 0:
        return MAXLEV
    sigma = rv * math.sqrt(BACKSTOP_BARS)
    maint = 1.0 / (2 * MAXLEV)
    room = LIQ_SIGMA * sigma * (1.0 + maint) + maint
    if room <= 0:
        return MAXLEV
    return max(1, min(MAXLEV, int(1.0 / room)))


def num(row, key):
    v = (row.get(key) or "").strip()
    try:
        return float(v)
    except ValueError:
        return None


def main(argv):
    path = argv[1] if len(argv) > 1 else "trades_15m.csv"
    scenarios = []
    for a in argv[2:]:
        base, coll = a.split(":")
        scenarios.append((float(base), float(coll)))
    if not scenarios:
        scenarios = [(24.0, 387.0), (44.0, 713.0)]

    with open(path, newline="", encoding="utf-8", errors="ignore") as fh:
        rows = list(csv.DictReader(fh))
    recs = [(size_mult(num(r, "ats_ratio"), num(r, "pierce_pct")), lev_for(num(r, "rv")))
            for r in rows]
    n = len(recs)
    if not n:
        print("no trades in %s" % path)
        return 1

    levs = {}
    for _, L in recs:
        levs[L] = levs.get(L, 0) + 1
    at_cap = sum(1 for m, _ in recs if m >= MULT_MAX - 1e-9)
    at_1x = levs.get(1, 0)
    both = sum(1 for m, L in recs if m >= MULT_MAX - 1e-9 and L == 1)

    print("trades replayed : %d  (%s)" % (n, path))
    print("leverage set    : %s" % {k: levs[k] for k in sorted(levs)})
    print("at MULT_MAX 4.0 : %d (%.1f%%)   at 1x lev: %d (%.1f%%)   BOTH: %d"
          % (at_cap, 100.0 * at_cap / n, at_1x, 100.0 * at_1x / n, both))
    print()
    for base, coll in scenarios:
        marg = sorted(base * m / L for m, L in recs)
        worst, theo = marg[-1], base * MULT_MAX
        print("base $%-6.0f collateral $%-7.0f" % (base, coll))
        print("   margin/position  mean $%-7.2f p90 $%-7.2f p99 $%-7.2f worst $%.2f"
              % (sum(marg) / n, marg[int(n * .90)], marg[int(n * .99)], worst))
        print("   worst = %.1f%% of collateral   (theoretical %.0f = %.1f%%%s)"
              % (100.0 * worst / coll, theo, 100.0 * theo / coll,
                 ", REACHED" if worst >= theo - 1e-9 else ""))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
