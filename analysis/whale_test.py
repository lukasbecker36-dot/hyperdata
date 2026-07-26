#!/usr/bin/env python3
"""Do whale-driven moves fade harder? The corrected test.

This decides whether the live arm should keep `--size-by-ats`, which bets up to 3x
notional on spikes whose average print size is unusually large.

Why v1 (tape_flow.py) could not answer it
  It measured the SIGN of large-print order flow. "Large" = top 20% of prints, so a
  5m bar holds only 1-2 of them and that sign collapses to +/-1 -- the deciles stopped
  being deciles and per-coin agreement was uncomputable. Suggestive, not evidence.

What is different here
  1. SHARE not sign. whale_share = large-print notional / total notional. A ratio of
     notionals is meaningful even with one large print, so it cannot degenerate.
  2. 15m bars, matching the strategy, so more prints per bar.
  3. A tape-native rebuild of the bot's own signal: ats_ratio = (mean print notional
     this bar) / (trailing 24h median of that). Same formula the bot uses, but from
     real per-print sizes instead of the candle proxy (volume / trade count).
  4. The premise is tested as P&L, not correlation. The strategy fades, so define
        fade_ret = -sign(OFI) * forward_return
     i.e. the return to selling into buying pressure and buying into selling pressure.
     "Whale spikes fade harder" is then exactly: does fade_ret RISE with ats_ratio?
  5. Reported on all bars AND on high-volume bars only, since the arm only ever trades
     volume spikes -- the traded population, not the average bar.

  python3 analysis/whale_test.py [bar_minutes]
"""
import csv, gzip, glob, math, random, sys
from collections import defaultdict, deque

TAPE_GLOB = "tape/tape_*.csv*"
BAR_MIN   = int(sys.argv[1]) if len(sys.argv) > 1 else 15
BAR_MS    = BAR_MIN * 60 * 1000
LARGE_PCT = 0.80
RESERVOIR = 20000
TRAIL     = max(8, int(24 * 60 / BAR_MIN))     # 24h of bars
HORIZONS  = [1, 2, 4]
MIN_TRADES = 20                                # need enough prints for a flow reading
VSPIKE     = 3.0                               # "high volume" cut for the traded-population view
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


# ---- pass 1: per-coin large-print cutoff ----
samp, seen = defaultdict(list), defaultdict(int)
for t, coin, side, px, sz in rows():
    ntl = px * sz
    s = samp[coin]; seen[coin] += 1
    if len(s) < RESERVOIR: s.append(ntl)
    else:
        j = random.randint(0, seen[coin] - 1)
        if j < RESERVOIR: s[j] = ntl
cut = {}
for coin, s in samp.items():
    if len(s) >= 200:
        s.sort(); cut[coin] = s[int(LARGE_PCT * len(s))]
print(f"pass 1: {sum(seen.values()):,} prints, {len(cut)} coins with a p80 cutoff")

# ---- pass 2: bars ----
# [buy, sell, ntl, lg_ntl, n, last_px]
bars = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0, 0.0])
for t, coin, side, px, sz in rows():
    c = cut.get(coin)
    if c is None: continue
    ntl = px * sz
    b = bars[(coin, t // BAR_MS)]
    if side == "B": b[0] += ntl
    else:           b[1] += ntl
    b[2] += ntl
    if ntl >= c:    b[3] += ntl
    b[4] += 1
    b[5] = px
print(f"pass 2: {len(bars):,} coin-bars at {BAR_MIN}m")

by_coin = defaultdict(dict)
for (coin, bi), v in bars.items():
    by_coin[coin][bi] = v


def median(xs):
    s = sorted(xs); n = len(s)
    return s[n//2] if n % 2 else 0.5*(s[n//2-1]+s[n//2])


# ---- build observations with trailing context ----
obs = []   # dict per observation
for coin, d in by_coin.items():
    idxs = sorted(d)
    hist_ntl, hist_aps = deque(maxlen=TRAIL), deque(maxlen=TRAIL)
    for bi in idxs:
        v = d[bi]
        aps = v[2] / v[4] if v[4] else 0.0          # avg print notional this bar
        if len(hist_ntl) >= TRAIL // 2 and v[4] >= MIN_TRADES and v[5] > 0:
            mn, ma = median(hist_ntl), median(hist_aps)
            tot = v[0] + v[1]
            if mn > 0 and ma > 0 and tot > 0:
                ofi = (v[0] - v[1]) / tot
                fwd = {}
                for h in HORIZONS:
                    nx = d.get(bi + h)
                    if nx and nx[5] > 0:
                        fwd[h] = (nx[5]/v[5] - 1.0) * 1e4
                if fwd and ofi != 0:
                    obs.append(dict(coin=coin, ofi=ofi, fwd=fwd,
                                    vratio=v[2]/mn,
                                    ats=aps/ma,                    # tape-native ats_ratio
                                    wshare=v[3]/v[2] if v[2] > 0 else 0.0))
        hist_ntl.append(v[2]); hist_aps.append(aps)
print(f"observations with 24h context: {len(obs):,}\n")


def st(xs):
    n = len(xs)
    if n < 2: return (float('nan'), float('nan'), n)
    m = sum(xs)/n
    sd = (sum((x-m)**2 for x in xs)/(n-1))**0.5
    return (m, m/(sd/math.sqrt(n)) if sd > 0 else float('nan'), n)


def fade(o, h):
    """P&L of the fade: sell into buying pressure, buy into selling pressure."""
    return -(1 if o["ofi"] > 0 else -1) * o["fwd"][h]


def quintile_table(key, label, pool, h, nb=5):
    pts = [o for o in pool if h in o["fwd"]]
    if len(pts) < 500:
        print(f"  {label}: too few ({len(pts)})"); return None
    pts.sort(key=lambda o: o[key])
    k = len(pts)//nb
    print(f"  fade return by {label}  (+{h*BAR_MIN}m, n={len(pts):,})")
    print(f"    {'bucket':>6} {key+' range':>18} {'fade bps':>10} {'t':>7} {'win%':>6}")
    out = []
    for i in range(nb):
        seg = pts[i*k:(i+1)*k] if i < nb-1 else pts[(nb-1)*k:]
        fr = [fade(o, h) for o in seg]
        m, t, n = st(fr)
        wr = sum(1 for x in fr if x > 0)/len(fr)*100
        out.append((seg[0][key], seg[-1][key], m, t, seg))
        print(f"    {i+1:>6} {seg[0][key]:>8.2f}..{seg[-1][key]:>7.2f} {m:>+10.2f} {t:>+7.1f} {wr:>5.0f}%")
    lo, hi = out[0], out[-1]
    diff = hi[2] - lo[2]
    # per-coin agreement on the direction of top-minus-bottom
    per = defaultdict(lambda: [[], []])
    for o in lo[4]: per[o["coin"]][0].append(fade(o, h))
    for o in hi[4]: per[o["coin"]][1].append(fade(o, h))
    sg = [sum(b)/len(b) - sum(a)/len(a) for a, b in per.values()
          if len(a) >= 10 and len(b) >= 10]
    agree = sum(1 for s in sg if (s > 0) == (diff > 0))/len(sg)*100 if sg else float('nan')
    verdict = ("SUPPORTS bigger size on high-" + key) if diff > 0 else \
              ("CONTRADICTS bigger size on high-" + key)
    print(f"    top minus bottom: {diff:+.2f} bps  -> {verdict}")
    print(f"    per-coin agreement: {agree:.0f}% of {len(sg)} coins\n")
    return diff


for h in HORIZONS:
    print("=" * 74)
    print(f"HORIZON +{h*BAR_MIN}m")
    print("=" * 74)
    print("-- ALL bars --")
    quintile_table("ats", "tape-native ats_ratio", obs, h)
    quintile_table("wshare", "whale share of notional", obs, h)
    spikes = [o for o in obs if o["vratio"] >= VSPIKE]
    print(f"-- HIGH-VOLUME bars only (vratio >= {VSPIKE}, the traded population) --")
    quintile_table("ats", "tape-native ats_ratio", spikes, h)
    quintile_table("wshare", "whale share of notional", spikes, h)

print("Note: fade return is gross. A maker round trip costs ~3bps and a taker one ~9bps,")
print("so a bucket has to clear that before it is tradeable, and the top-minus-bottom")
print("spread has to be large enough to justify the size differential on top of that.")
