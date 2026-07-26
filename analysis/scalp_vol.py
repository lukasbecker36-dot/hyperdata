#!/usr/bin/env python3
"""Is there a short-horizon (minutes) scalp in volume spikes?

The existing strategy fades volume spikes at a 24h-range breakout and holds ~8h. This
asks a different question: forget the breakout and the 8h hold -- does an extreme volume
spike revert enough over the NEXT FEW MINUTES to pay for itself?

Why it might not: tape_flow.py already found order-flow reversion of only ~1-1.6bps at
5-30m, which is inside a 3bps maker round trip. But that was the average across flow
deciles. Conditioning harder on volume made the effect much larger in whale_test.py
(+4.9 to +8bps), so the tail is worth checking properly.

Setup
  1m bars per coin from the tape. A spike is a bar whose notional is >= N x its trailing
  60-minute median. The trade fades the spike bar's own move: if price rose during the
  bar, short it; if it fell, buy it. Exit at a fixed horizon.

Costs are the whole question here, so all three are shown:
  gross        no costs
  net maker    -3bps round trip, and you may not get filled (today's audit: ~76%)
  net taker    -9bps round trip, but you always get in
A scalp needs to clear NET TAKER to be reliable, or clear net maker and accept
that a quarter of the entries never happen.

Also reports events/day, because an edge you only see 3 times a week is not a strategy.

  python3 analysis/scalp_vol.py [bar_minutes]
"""
import csv, gzip, glob, math, sys
from collections import defaultdict, deque

TAPE_GLOB = "tape/tape_*.csv*"
BAR_MIN   = int(sys.argv[1]) if len(sys.argv) > 1 else 1
BAR_MS    = BAR_MIN * 60 * 1000
TRAIL     = max(20, int(60 / BAR_MIN))          # ~1h of trailing bars
HORIZONS  = [1, 2, 5, 10]                       # in bars
MIN_TRADES = 10
BUCKETS   = [(3, 5), (5, 10), (10, 20), (20, 50), (50, 1e9)]
MAKER_RT, TAKER_RT = 3.0, 9.0


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


# bar -> [ntl, buy, sell, n, first_px, last_px]
bars = defaultdict(lambda: [0.0, 0.0, 0.0, 0, 0.0, 0.0])
tmin = tmax = None
for t, coin, side, px, sz in rows():
    tmin = t if tmin is None else min(tmin, t)
    tmax = t if tmax is None else max(tmax, t)
    b = bars[(coin, t // BAR_MS)]
    ntl = px * sz
    b[0] += ntl
    if side == "B": b[1] += ntl
    else:           b[2] += ntl
    b[3] += 1
    if b[4] == 0.0: b[4] = px
    b[5] = px
days = (tmax - tmin) / 86400000.0
print(f"{len(bars):,} coin-bars at {BAR_MIN}m over {days:.1f} days")

by_coin = defaultdict(dict)
for (coin, bi), v in bars.items():
    by_coin[coin][bi] = v


def median(xs):
    s = sorted(xs); n = len(s)
    return s[n//2] if n % 2 else 0.5*(s[n//2-1]+s[n//2])


ev = []
for coin, d in by_coin.items():
    hist = deque(maxlen=TRAIL)
    for bi in sorted(d):
        v = d[bi]
        if len(hist) >= TRAIL//2 and v[3] >= MIN_TRADES and v[4] > 0 and v[5] > 0:
            m = median(hist)
            if m > 0:
                vr = v[0]/m
                move = (v[5]/v[4] - 1.0)            # the spike bar's own move
                if vr >= BUCKETS[0][0] and move != 0:
                    dirn = -1 if move > 0 else 1     # fade it
                    fwd = {}
                    for h in HORIZONS:
                        nx = d.get(bi+h)
                        if nx and nx[5] > 0:
                            fwd[h] = dirn*(nx[5]/v[5]-1.0)*1e4
                    if fwd:
                        tot = v[1]+v[2]
                        ev.append(dict(coin=coin, vr=vr, fwd=fwd,
                                       move_bps=move*1e4,
                                       ofi=(v[1]-v[2])/tot if tot > 0 else 0.0))
        hist.append(v[0])
print(f"spike events (vratio>={BUCKETS[0][0]}): {len(ev):,}  "
      f"= {len(ev)/days:,.0f}/day across {len(by_coin)} coins\n")
if len(ev) < 300:
    print("too few events"); sys.exit(0)


def st(xs):
    n = len(xs)
    if n < 2: return (float('nan'), float('nan'), n)
    m = sum(xs)/n
    sd = (sum((x-m)**2 for x in xs)/(n-1))**0.5
    return (m, m/(sd/math.sqrt(n)) if sd > 0 else float('nan'), n)


for h in HORIZONS:
    print("=" * 86)
    print(f"HOLD {h*BAR_MIN} MINUTE(S)")
    print("=" * 86)
    print(f"  {'vol spike':>12} {'n':>6} {'/day':>6} {'gross':>8} {'t':>6} "
          f"{'net maker':>10} {'net taker':>10} {'win%':>6} {'coins agree':>12}")
    for lo, hi in BUCKETS:
        seg = [e for e in ev if lo <= e["vr"] < hi and h in e["fwd"]]
        if len(seg) < 30:
            continue
        r = [e["fwd"][h] for e in seg]
        m, t, n = st(r)
        per = defaultdict(list)
        for e in seg: per[e["coin"]].append(e["fwd"][h])
        sg = [sum(v)/len(v) for v in per.values() if len(v) >= 10]
        agree = (sum(1 for s in sg if s > 0)/len(sg)*100) if sg else float('nan')
        lab = f"{lo:g}-{hi:g}x" if hi < 1e9 else f"{lo:g}x+"
        print(f"  {lab:>12} {n:>6} {n/days:>6.0f} {m:>+8.2f} {t:>+6.1f} "
              f"{m-MAKER_RT:>+10.2f} {m-TAKER_RT:>+10.2f} "
              f"{sum(1 for x in r if x>0)/len(r)*100:>5.0f}% "
              f"{agree:>11.0f}%")
    print()

print("Reading it: 'net taker' is the honest number for a scalp, because a maker entry")
print("that does not fill is not a trade -- and today's audit put maker entry fills at")
print("~76%. A bucket has to be positive there, on enough events per day to matter, and")
print("agreed on by most coins, before it is worth building.")
