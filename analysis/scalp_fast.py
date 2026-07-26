#!/usr/bin/env python3
"""Does a volume spike CONTINUE if you get in fast enough? (seconds, not minutes)

scalp_vol.py measured from the spike bar's CLOSE and found fading was weakly positive
(+0.4 to +2.5bps gross), which means momentum -- going WITH the spike -- was the exact
negative. But that test enters after the move is already over, so it cannot see
continuation inside the first seconds.

This measures at tape resolution. For each spike minute, anchor on the LAST print of the
minute and look forward 5/10/15/30/60/120 seconds, measuring the move in the spike's own
direction:
    momentum_bps > 0  price kept going  -> going long an up-spike works
    momentum_bps < 0  price reverted    -> fading works, momentum loses

Costs are the whole story. Reacting in seconds means TAKING, not resting, so the bar is a
~9bps taker round trip. It is shown alongside so the comparison is honest rather than
gross-only. A maker column is meaningless here: if you could rest passively you would not
need to be fast.

  python3 analysis/scalp_fast.py [vol_mult]
"""
import bisect, csv, gzip, glob, math, sys
from collections import defaultdict, deque

TAPE_GLOB = "tape/tape_*.csv*"
BAR_MS    = 60 * 1000
TRAIL     = 60                        # trailing minutes for the volume median
OFFSETS_S = [5, 10, 15, 30, 60, 120]
MIN_TRADES = 10
VOL_MULT  = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
BUCKETS   = [(5, 10), (10, 20), (20, 50), (50, 1e9)]
TAKER_RT  = 9.0


def prints():
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


# ---- pass 1: 1m bars, to find spikes and their direction ----
bars = defaultdict(lambda: [0.0, 0, 0.0, 0.0, 0])     # ntl, n, first, last, last_t
for t, coin, side, px, sz in prints():
    b = bars[(coin, t // BAR_MS)]
    b[0] += px * sz
    b[1] += 1
    if b[2] == 0.0: b[2] = px
    b[3] = px
    b[4] = t
print(f"{len(bars):,} coin-minutes")

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
        if len(hist) >= TRAIL//2 and v[1] >= MIN_TRADES and v[2] > 0 and v[3] > 0:
            m = median(hist)
            if m > 0:
                vr = v[0]/m
                move = v[3]/v[2] - 1.0
                if vr >= VOL_MULT and move != 0:
                    ev.append(dict(coin=coin, t_end=v[4], px=v[3], vr=vr,
                                   sgn=1 if move > 0 else -1,
                                   move_bps=move*1e4, fut={}))
        hist.append(v[0])
print(f"spikes (vratio>={VOL_MULT}): {len(ev):,}")
if len(ev) < 300:
    print("too few"); sys.exit(0)

# ---- pass 2: price at t_end + offset, streaming ----
# For each coin keep its events sorted by t_end and a pointer; the price AT a deadline is
# the last print strictly before the first print that exceeds it.
per = defaultdict(list)
for i, e in enumerate(ev):
    per[e["coin"]].append((e["t_end"], i))
for c in per:
    per[c].sort()
last_px = {}
# pending[coin] = list of [deadline, event_idx, offset]
pending = defaultdict(deque)
ptr = {c: 0 for c in per}
maxoff = max(OFFSETS_S) * 1000
for t, coin, side, px, sz in prints():
    lst = per.get(coin)
    if lst is None:
        continue
    # arm any events whose t_end has passed
    p = ptr[coin]
    while p < len(lst) and lst[p][0] <= t:
        i = lst[p][1]
        for off in OFFSETS_S:
            pending[coin].append([ev[i]["t_end"] + off*1000, i, off])
        p += 1
    ptr[coin] = p
    # resolve deadlines that this print has passed, using the PREVIOUS last price
    q = pending[coin]
    lp = last_px.get(coin)
    while q and q[0][0] < t:
        dl, i, off = q.popleft()
        if lp is not None:
            ev[i]["fut"][off] = lp
    # drop hopeless stragglers
    last_px[coin] = px
print("pass 2 done\n")


def st(xs):
    n = len(xs)
    if n < 2: return (float('nan'), float('nan'), n)
    m = sum(xs)/n
    sd = (sum((x-m)**2 for x in xs)/(n-1))**0.5
    return (m, m/(sd/math.sqrt(n)) if sd > 0 else float('nan'), n)


print("MOMENTUM return: positive = the spike KEPT GOING (long the spike works)")
print("                 negative = it reverted (fading works)\n")
print(f"  {'spike':>10} {'after':>7} {'n':>7} {'momentum bps':>13} {'t':>7} "
      f"{'net taker':>10} {'win%':>6}")
for lo, hi in BUCKETS:
    lab = f"{lo:g}-{hi:g}x" if hi < 1e9 else f"{lo:g}x+"
    for off in OFFSETS_S:
        seg = [e for e in ev if lo <= e["vr"] < hi and off in e["fut"] and e["px"] > 0]
        if len(seg) < 50:
            continue
        r = [e["sgn"] * (e["fut"][off]/e["px"] - 1.0) * 1e4 for e in seg]
        m, tt, n = st(r)
        print(f"  {lab:>10} {str(off)+'s':>7} {n:>7} {m:>+13.2f} {tt:>+7.1f} "
              f"{m-TAKER_RT:>+10.2f} {sum(1 for x in r if x>0)/len(r)*100:>5.0f}%")
    print()
print("Note: a positive momentum number still needs to clear ~9bps of taker round trip,")
print("because reacting within seconds means crossing the spread on both sides. And the")
print("anchor price is the spike's LAST PRINT -- in reality you would be a few hundred")
print("milliseconds and a spread behind that, so these figures are optimistic.")
