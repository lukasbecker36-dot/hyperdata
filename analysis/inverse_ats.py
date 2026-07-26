#!/usr/bin/env python3
"""Should we oversize CROWD spikes (many small prints) instead of WHALE spikes?

whale_test.py found that among volume spikes, fade returns fall as average print size
rises -- the opposite of the live arm's `--size-by-ats`. This asks the follow-up: is the
inverse rule actually a strategy, or an artifact?

Three things it has to survive.

1. A REAL event definition. Earlier tests fired on any high-volume bar and used order-flow
   sign as the trade direction. Here the event is the strategy's own: volume spike AND a
   breakout of the trailing 24h range, faded in the breakout direction (short an up-break,
   long a down-break). Same shape as paper_bot.features().

2. COSTS. Gross bps means nothing. Every number below is also shown net of a 3bps maker
   round trip, size-weighted, because a 3x bet pays 3x the cost.

3. The CONFOUND that matters. Low average print size may just be a property of certain
   coins, not of certain spikes. If so this is "trade these coins", not "size up on these
   spikes" -- a completely different (and much weaker) claim, since coin selection is
   already covered by the MID-tier work. So the ats signal is split into:
       between-coin : the coin's own average ats across all its spikes
       within-coin  : this spike's ats minus that coin's average
   If the edge lives in the within-coin part, it is genuinely a spike-character signal
   and an inverse sizing rule makes sense. If it lives between coins, it does not.

  python3 analysis/inverse_ats.py [bar_minutes] [vol_mult]
"""
import csv, gzip, glob, math, random, sys
from collections import defaultdict, deque

TAPE_GLOB = "tape/tape_*.csv*"
BAR_MIN   = int(sys.argv[1]) if len(sys.argv) > 1 else 15
VOL_MULT  = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
BAR_MS    = BAR_MIN * 60 * 1000
TRAIL     = max(8, int(24 * 60 / BAR_MIN))
HORIZONS  = [4, 16, 32]                     # 1h / 4h / 8h at 15m bars (8h = the backstop)
MIN_TRADES = 20
RESERVOIR = 20000
COST_BPS  = 3.0                             # maker round trip
SIZE_REF, SIZE_MIN, SIZE_MAX = 2.0, 0.5, 3.0    # the bot's own clamp
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
    if len(s) < RESERVOIR: s.append(px*sz)
    else:
        j = random.randint(0, seen[coin]-1)
        if j < RESERVOIR: s[j] = px*sz
print(f"{sum(seen.values()):,} prints, {len(samp)} coins")

# bar -> [ntl, lg_ntl, n, last_px, hi, lo]
bars = defaultdict(lambda: [0.0, 0.0, 0, 0.0, 0.0, 1e30])
cut = {}
for coin, s in samp.items():
    if len(s) >= 200:
        s.sort(); cut[coin] = s[int(0.80*len(s))]
for t, coin, side, px, sz in rows():
    c = cut.get(coin)
    if c is None: continue
    ntl = px*sz
    b = bars[(coin, t//BAR_MS)]
    b[0] += ntl
    if ntl >= c: b[1] += ntl
    b[2] += 1; b[3] = px
    if px > b[4]: b[4] = px
    if px < b[5]: b[5] = px
by_coin = defaultdict(dict)
for (coin, bi), v in bars.items():
    by_coin[coin][bi] = v
print(f"{len(bars):,} coin-bars at {BAR_MIN}m")


def median(xs):
    s = sorted(xs); n = len(s)
    return s[n//2] if n % 2 else 0.5*(s[n//2-1]+s[n//2])


# ---- events: volume spike AND 24h range breakout, faded ----
ev = []
for coin, d in by_coin.items():
    idxs = sorted(d)
    h_ntl, h_aps, h_hi, h_lo = deque(maxlen=TRAIL), deque(maxlen=TRAIL), deque(maxlen=TRAIL), deque(maxlen=TRAIL)
    for bi in idxs:
        v = d[bi]
        aps = v[0]/v[2] if v[2] else 0.0
        if len(h_ntl) >= TRAIL//2 and v[2] >= MIN_TRADES and v[3] > 0:
            mn, ma = median(h_ntl), median(h_aps)
            ph, pl = max(h_hi), min(h_lo)
            if mn > 0 and ma > 0:
                vratio = v[0]/mn
                brk = 1 if v[3] > ph else (-1 if v[3] < pl else 0)
                if vratio >= VOL_MULT and brk != 0:
                    dirn = -brk                       # fade it
                    fwd = {}
                    for h in HORIZONS:
                        nx = d.get(bi+h)
                        if nx and nx[3] > 0:
                            fwd[h] = dirn*(nx[3]/v[3]-1.0)*1e4
                    if fwd:
                        ev.append(dict(coin=coin, ats=aps/ma, vratio=vratio, fwd=fwd,
                                       wshare=v[1]/v[0] if v[0] > 0 else 0.0))
        h_ntl.append(v[0]); h_aps.append(aps); h_hi.append(v[4]); h_lo.append(v[5])
print(f"spike+breakout events (vratio>={VOL_MULT}): {len(ev):,}\n")
if len(ev) < 200:
    print("too few events to say anything; lower vol_mult or wait for more tape"); sys.exit(0)

# between/within decomposition of the ats signal
cmean = defaultdict(list)
for e in ev: cmean[e["coin"]].append(e["ats"])
cavg = {c: sum(v)/len(v) for c, v in cmean.items()}
for e in ev:
    e["ats_between"] = cavg[e["coin"]]
    e["ats_within"] = e["ats"] - cavg[e["coin"]]


def st(xs):
    n = len(xs)
    if n < 2: return (float('nan'), float('nan'), n)
    m = sum(xs)/n
    sd = (sum((x-m)**2 for x in xs)/(n-1))**0.5
    return (m, m/(sd/math.sqrt(n)) if sd > 0 else float('nan'), n)


def buckets(key, label, h, nb=4):
    pts = [e for e in ev if h in e["fwd"]]
    pts.sort(key=lambda e: e[key])
    k = len(pts)//nb
    print(f"  {label}  (+{h*BAR_MIN//60}h, n={len(pts):,})")
    print(f"    {'q':>3} {'range':>16} {'gross bps':>10} {'t':>6} {'win%':>6}")
    out = []
    for i in range(nb):
        seg = pts[i*k:(i+1)*k] if i < nb-1 else pts[(nb-1)*k:]
        r = [e["fwd"][h] for e in seg]
        m, t, n = st(r)
        out.append(m)
        print(f"    {i+1:>3} {seg[0][key]:>7.2f}..{seg[-1][key]:>6.2f} {m:>+10.2f} {t:>+6.1f} "
              f"{sum(1 for x in r if x>0)/len(r)*100:>5.0f}%")
    print(f"    bottom minus top: {out[0]-out[-1]:+.2f} bps"
          f"   ({'small-print spikes fade BETTER' if out[0]>out[-1] else 'big-print spikes fade better'})\n")


def sim(h):
    """Size-weighted net P&L per rule. Cost scales with size, as it does in reality."""
    pts = [e for e in ev if h in e["fwd"]]
    rules = {
        "flat 1.0x        ": lambda e: 1.0,
        "ats (live bot)   ": lambda e: min(SIZE_MAX, max(SIZE_MIN, e["ats"]/SIZE_REF)),
        "INVERSE ats      ": lambda e: min(SIZE_MAX, max(SIZE_MIN, SIZE_REF/e["ats"])),
    }
    print(f"  size-weighted, net of {COST_BPS}bps maker round trip   (+{h*BAR_MIN//60}h, n={len(pts):,})")
    print(f"    {'rule':>18} {'avg size':>9} {'net bps/unit':>13} {'total (size*bps)':>17} {'t':>7}")
    for name, f in rules.items():
        per = [f(e)*(e["fwd"][h]-COST_BPS) for e in pts]
        sizes = [f(e) for e in pts]
        m, t, n = st(per)
        tot = sum(per)
        print(f"    {name} {sum(sizes)/len(sizes):>9.2f} {tot/sum(sizes):>13.2f} {tot:>17.0f} {t:>+7.1f}")
    print()


for h in HORIZONS:
    print("=" * 78)
    print(f"HORIZON +{h*BAR_MIN//60}h")
    print("=" * 78)
    buckets("ats", "fade return by ats_ratio (raw)      ", h)
    buckets("ats_within", "by WITHIN-coin ats (spike character)", h)
    buckets("ats_between", "by BETWEEN-coin ats (coin selection)", h)
    sim(h)

print("How to read this: if the WITHIN-coin gradient is the strong one, spike character is")
print("real and an inverse sizing rule is justified. If BETWEEN-coin is the strong one, this")
print("is really a coin filter and overlaps what the MID-tier work already found -- sizing")
print("would be the wrong way to express it.")
