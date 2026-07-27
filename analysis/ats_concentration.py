#!/usr/bin/env python3
"""Is the evidence AGAINST ats sizing itself concentrated?

The regime-filter retraction (946914b) showed a headline effect that was 83-97% a single
month. That same scrutiny was never applied to the evidence used to call ats sizing
unproven, which is not consistent. This applies it.

The anti-ats case rests on analysis/inverse_ats.py: on tape-derived spike+breakout events,
size-weighted and net of a 3bps maker round trip, the ranking was inverse > flat > ats at
every horizon. If that gap is carried by a handful of events or a single day, it deserves no
more weight than the January artefact did.

Checks:
  1. per-DAY totals for each sizing rule (the tape is only ~5 days, so days are the
     finest honest slice available)
  2. concentration -- what share of the ats-minus-flat gap comes from the few largest
     contributors, and does the ranking survive dropping them
  3. the same for the ats-minus-inverse gap

  python3 analysis/ats_concentration.py [bar_minutes] [vol_mult]
"""
import csv, gzip, glob, math, random, sys
from collections import defaultdict, deque
from datetime import datetime, timezone

TAPE_GLOB = "tape/tape_*.csv*"
BAR_MIN = int(sys.argv[1]) if len(sys.argv) > 1 else 15
VOL_MULT = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
BAR_MS = BAR_MIN * 60 * 1000
TRAIL = max(8, int(24 * 60 / BAR_MIN))
HOLD = 32                       # 8h at 15m -- the strategy's backstop
MIN_TRADES = 20
RESERVOIR = 20000
COST_BPS = 3.0
SIZE_REF, SIZE_MIN, SIZE_MAX = 2.0, 0.5, 3.0
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


samp, seen = defaultdict(list), defaultdict(int)
for t, coin, side, px, sz in rows():
    s = samp[coin]; seen[coin] += 1
    if len(s) < RESERVOIR:
        s.append(px*sz)
    else:
        j = random.randint(0, seen[coin]-1)
        if j < RESERVOIR:
            s[j] = px*sz
cut = {}
for c, v in samp.items():
    if len(v) >= 200:
        v.sort(); cut[c] = v[int(0.8*len(v))]

bars = defaultdict(lambda: [0.0, 0.0, 0, 0.0, 0.0, 1e30])   # ntl, lg, n, last, hi, lo
for t, coin, side, px, sz in rows():
    if coin not in cut:
        continue
    ntl = px*sz
    b = bars[(coin, t // BAR_MS)]
    b[0] += ntl
    if ntl >= cut[coin]: b[1] += ntl
    b[2] += 1; b[3] = px
    if px > b[4]: b[4] = px
    if px < b[5]: b[5] = px
by = defaultdict(dict)
for (c, i), v in bars.items():
    by[c][i] = v


def median(xs):
    s = sorted(xs); n = len(s)
    return s[n//2] if n % 2 else 0.5*(s[n//2-1]+s[n//2])


def clamp(x):
    return min(SIZE_MAX, max(SIZE_MIN, x))


ev = []
for coin, d in by.items():
    idx = sorted(d)
    hn, ha, hh, hl = (deque(maxlen=TRAIL) for _ in range(4))
    for i in idx:
        v = d[i]
        aps = v[0]/v[2] if v[2] else 0.0
        if len(hn) >= TRAIL//2 and v[2] >= MIN_TRADES and v[3] > 0:
            mn, ma = median(hn), median(ha)
            ph, pl = max(hh), min(hl)
            if mn > 0 and ma > 0:
                vr = v[0]/mn
                brk = 1 if v[3] > ph else (-1 if v[3] < pl else 0)
                if vr >= VOL_MULT and brk != 0:
                    nx = d.get(i + HOLD)
                    if nx and nx[3] > 0:
                        r = (-brk)*(nx[3]/v[3]-1.0)*1e4
                        ev.append(dict(coin=coin, ms=i*BAR_MS, ats=aps/ma, ret=r))
        hn.append(v[0]); ha.append(aps); hh.append(v[4]); hl.append(v[5])
print(f"events: {len(ev):,} (vratio>={VOL_MULT}, {BAR_MIN}m bars, {HOLD} bar hold)")
if len(ev) < 200:
    print("too few"); sys.exit(0)

RULES = {"flat": lambda e: 1.0,
         "ats": lambda e: clamp(e["ats"]/SIZE_REF),
         "inverse": lambda e: clamp(SIZE_REF/e["ats"]) if e["ats"] > 0 else 1.0}
for e in ev:
    for k, f in RULES.items():
        e[k] = f(e)*(e["ret"] - COST_BPS)       # size-weighted, cost scales with size
    e["day"] = datetime.fromtimestamp(e["ms"]/1000, tz=timezone.utc).strftime("%m-%d")


def st(xs):
    n = len(xs)
    if n < 2: return (float("nan"), float("nan"))
    m = sum(xs)/n
    sd = (sum((x-m)**2 for x in xs)/(n-1))**0.5
    return m, (m/(sd/math.sqrt(n)) if sd > 0 else float("nan"))


tot = {k: sum(e[k] for e in ev) for k in RULES}
print(f"\n=== full sample (what inverse_ats.py reported) ===")
print(f"  {'rule':>9} {'total':>11} {'per trade':>11} {'t':>7}")
for k in RULES:
    m, t = st([e[k] for e in ev])
    print(f"  {k:>9} {tot[k]:>+11,.0f} {m:>+11.2f} {t:>+7.1f}")
print(f"  ats minus flat: {tot['ats']-tot['flat']:+,.0f}     "
      f"ats minus inverse: {tot['ats']-tot['inverse']:+,.0f}")

print(f"\n=== per DAY (the finest honest slice -- the tape is only ~5 days) ===")
print(f"  {'day':>7} {'n':>5} {'flat':>10} {'ats':>10} {'inverse':>10} "
      f"{'ats-flat':>10} {'ats worst?':>11}")
days = sorted({e["day"] for e in ev})
worst = 0
for dy in days:
    g = [e for e in ev if e["day"] == dy]
    if len(g) < 20:
        continue
    f_, a_, i_ = (sum(e[k] for e in g) for k in ("flat", "ats", "inverse"))
    w = a_ < f_ and a_ < i_
    worst += 1 if w else 0
    print(f"  {dy:>7} {len(g):>5} {f_:>+10,.0f} {a_:>+10,.0f} {i_:>+10,.0f} "
          f"{a_-f_:>+10,.0f} {'yes' if w else 'no':>11}")
print(f"  ats was worst of the three on {worst} of "
      f"{len([d for d in days if len([e for e in ev if e['day']==d])>=20])} days")

print(f"\n=== concentration of the ats-minus-flat gap ===")
gaps = sorted(((e["ats"]-e["flat"]), e) for e in ev)
gap = tot["ats"] - tot["flat"]
print(f"  full gap: {gap:+,.0f} bps")
for k in (1, 3, 5, 10, 20):
    worst_k = sum(g for g, _ in gaps[:k])
    print(f"    the {k:>2} most ats-damaging events contribute {worst_k:>+9,.0f} "
          f"= {worst_k/gap*100:>5.0f}% of it")
for k in (5, 10, 20, 50):
    keep = [e for _, e in gaps[k:]]
    tf, ta, ti = (sum(e[x] for e in keep) for x in ("flat", "ats", "inverse"))
    rank = " > ".join(sorted(("flat", "ats", "inverse"),
                             key=lambda x: -{"flat": tf, "ats": ta, "inverse": ti}[x]))
    print(f"  dropping the {k:>2} worst-for-ats events -> ranking {rank}")
print()
print("If the ranking flips once a handful of events are dropped, or ats is only worst on")
print("one or two days, then the anti-ats case is the same kind of artefact as the January")
print("regime result and should carry no more weight than the 57 paper trades that favour it.")
