#!/usr/bin/env python3
"""Mine the trade tape for what candles cannot see: aggressor side and per-trade size.

The tape (time_ms, coin, side, px, sz, tid) carries two things no candle has:
  1. side  -- B = buy-aggressor, A = sell-aggressor. Signed order flow, directly.
  2. sz    -- per-print size, so "was this one whale or a thousand retail clicks?"
              is answerable instead of approximated.

Item 2 is the live arm's whole premise. `--size-by-ats` scales notional by
(bar_volume / bar_trade_count) vs its trailing median -- a candle-only proxy for
average print size, chosen on 57 paper trades. The tape lets us test the underlying
claim properly: does flow from LARGE prints behave differently from flow from small
prints? That is whale-vs-crowd stated precisely, and there are ~7.8M prints to test
it on rather than 57 trades.

Method
  pass 1  per-coin reservoir sample of print notionals -> p80 = "large print" cutoff
  pass 2  aggregate to BAR_MIN bars per coin, splitting signed notional into
          large-print and small-print buckets
  then    order-flow imbalance per bar, OFI = (buy - sell) / (buy + sell), computed
          three ways (all / large-only / small-only), bucketed into deciles against
          forward returns at +1, +3, +6 bars

Reading the result: this strategy FADES. So the edge we want is REVERSION -- positive
flow now followed by NEGATIVE forward return. A monotone decile curve sloping down is
the fade edge; sloping up would mean flow is momentum and fading it is wrong.

Caveats printed with the output: observations overlap across coins at the same instant
(market-wide moves), so t-stats are optimistic. Per-coin sign consistency is reported
as the robustness check that does not care about cross-sectional correlation.

  python3 analysis/tape_flow.py [bar_minutes]
"""
import csv, gzip, glob, math, os, random, sys
from collections import defaultdict

TAPE_GLOB = "tape/tape_*.csv*"
BAR_MIN   = int(sys.argv[1]) if len(sys.argv) > 1 else 5
BAR_MS    = BAR_MIN * 60 * 1000
LARGE_PCT = 0.80          # a print above its coin's p80 notional counts as "large"
RESERVOIR = 20000
HORIZONS  = [1, 3, 6]     # in bars
MIN_TRADES = 5            # ignore near-empty bars (no meaningful flow reading)
random.seed(11)


def rows():
    for tf in sorted(glob.glob(TAPE_GLOB)):
        op = gzip.open if tf.endswith(".gz") else open
        with op(tf, "rt") as f:
            for r in csv.reader(f):
                if not r or r[0] == "time_ms" or len(r) < 5:
                    continue
                try:
                    yield int(r[0]), r[1], r[2], float(r[3]), float(r[4])
                except ValueError:
                    continue


# ---- pass 1: per-coin notional distribution ----
samp = defaultdict(list)
seen = defaultdict(int)
n_tot = 0
for t, coin, side, px, sz in rows():
    n_tot += 1
    ntl = px * sz
    s = samp[coin]; seen[coin] += 1
    if len(s) < RESERVOIR:
        s.append(ntl)
    else:                                    # reservoir sampling, unbiased
        j = random.randint(0, seen[coin] - 1)
        if j < RESERVOIR:
            s[j] = ntl
cut = {}
for coin, s in samp.items():
    if len(s) >= 200:
        s.sort()
        cut[coin] = s[int(LARGE_PCT * len(s))]
print(f"pass 1: {n_tot:,} prints, {len(samp)} coins, {len(cut)} with a usable p80 cutoff")

# ---- pass 2: bars ----
# bar -> [buy_all, sell_all, buy_lg, sell_lg, buy_sm, sell_sm, ntrades, last_px]
bars = defaultdict(lambda: [0.0]*6 + [0, 0.0])
for t, coin, side, px, sz in rows():
    c = cut.get(coin)
    if c is None:
        continue
    ntl = px * sz
    b = bars[(coin, t // BAR_MS)]
    buy = (side == "B")
    if buy: b[0] += ntl
    else:   b[1] += ntl
    if ntl >= c:
        if buy: b[2] += ntl
        else:   b[3] += ntl
    else:
        if buy: b[4] += ntl
        else:   b[5] += ntl
    b[6] += 1
    b[7] = px                                # last print in the bar = bar close
print(f"pass 2: {len(bars):,} coin-bars at {BAR_MIN}m")

by_coin = defaultdict(dict)
for (coin, bi), v in bars.items():
    by_coin[coin][bi] = v

# ---- assemble observations ----
obs = []            # (coin, ofi_all, ofi_lg, ofi_sm, {h: fwd_bps})
for coin, d in by_coin.items():
    for bi, v in d.items():
        if v[6] < MIN_TRADES or v[7] <= 0:
            continue
        fwd = {}
        for h in HORIZONS:
            nxt = d.get(bi + h)
            if nxt and nxt[7] > 0:
                fwd[h] = (nxt[7] / v[7] - 1.0) * 1e4
        if not fwd:
            continue
        def imb(a, b):
            s = a + b
            return None if s <= 0 else (a - b) / s
        obs.append((coin, imb(v[0], v[1]), imb(v[2], v[3]), imb(v[4], v[5]), fwd))
print(f"usable observations: {len(obs):,}\n")


def stats(xs):
    n = len(xs)
    if n < 2: return (float('nan'), float('nan'), n)
    m = sum(xs)/n
    sd = (sum((x-m)**2 for x in xs)/(n-1))**0.5
    return (m, m/(sd/math.sqrt(n)) if sd > 0 else float('nan'), n)


def decile_table(idx, label, h):
    pts = [(o[idx], o[4][h], o[0]) for o in obs if o[idx] is not None and h in o[4]]
    if len(pts) < 1000:
        print(f"  {label}: too few points ({len(pts)})"); return
    pts.sort(key=lambda x: x[0])
    k = len(pts)//10
    print(f"  {label} vs +{h*BAR_MIN}m forward return   (n={len(pts):,})")
    print(f"    {'decile':>7} {'OFI range':>16} {'mean fwd bps':>13} {'t':>7}")
    rows_out = []
    for d in range(10):
        seg = pts[d*k:(d+1)*k] if d < 9 else pts[9*k:]
        m, t, n = stats([p[1] for p in seg])
        rows_out.append((seg[0][0], seg[-1][0], m, t))
        print(f"    {d+1:>7} {seg[0][0]:>+7.2f}..{seg[-1][0]:>+7.2f} {m:>+13.2f} {t:>+7.1f}")
    # top-vs-bottom spread, and per-coin sign consistency of that spread
    lo = [p for p in pts[:k]]; hi = [p for p in pts[-k:]]
    ml, _, _ = stats([p[1] for p in lo]); mh, _, _ = stats([p[1] for p in hi])
    spread = mh - ml
    per = defaultdict(lambda: [[], []])
    for p in lo: per[p[2]][0].append(p[1])
    for p in hi: per[p[2]][1].append(p[1])
    signs = [ (sum(b)/len(b)) - (sum(a)/len(a))
              for a, b in per.values() if len(a) >= 20 and len(b) >= 20 ]
    agree = sum(1 for s in signs if (s < 0) == (spread < 0)) / len(signs) * 100 if signs else float('nan')
    print(f"    top-decile minus bottom-decile: {spread:+.2f} bps"
          f"   ({'REVERSION - fade works' if spread < 0 else 'MOMENTUM - fading is wrong'})")
    print(f"    per-coin agreement on that sign: {agree:.0f}% of {len(signs)} coins\n")


for h in HORIZONS:
    print(f"=== horizon +{h*BAR_MIN}m " + "=" * 40)
    decile_table(1, "OFI all prints  ", h)
    decile_table(2, "OFI LARGE prints", h)
    decile_table(3, "OFI small prints", h)

print("Caveats: observations overlap across coins at the same instant, so market-wide")
print("moves correlate them and the t-stats above are optimistic. The per-coin agreement")
print("line is the robustness check that is immune to that. Costs are NOT included --")
print("a spread of a few bps is inside the round-trip cost of ~3bps maker / ~9bps taker.")
