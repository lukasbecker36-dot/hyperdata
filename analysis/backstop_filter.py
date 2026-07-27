#!/usr/bin/env python3
"""Can the backstop losers be identified BEFORE entry?

take_profit.py decomposed the edge: 81% of trades reclaim for +155.5bps, 19% run to the
8h backstop for -394.9bps. All the leakage is in that second group, and capping winners
does not touch it (every take-profit level lost). So: is a backstop trade predictable at
signal time?

Two feature families, reported separately because their statistical power differs by an
order of magnitude:

  CANDLE features -- available for the whole 52-day sample (~1800 events)
    pierce_bps    how far the close pierced BEYOND the prior 24h range. Mechanically
                  relevant: reclaim requires price to close back inside that range, so a
                  deep pierce needs a bigger move to come back. Prime suspect.
    pierce_frac   the same, as a fraction of the range width (scale-free version)
    range_bps     prior 24h range width / price
    vratio, rv    spike size and realized vol
    ats_candle    (volume / num_trades) vs its trailing median -- the candle proxy the
                  live arm sizes on
    funding_abs   crowding magnitude, not just its sign

  TAPE features -- only the ~5 days the tape logger has been running (small sample)
    vpin60, adverse_ofi   flow toxicity (analysis/toxicity.py)
    whale_share           large-print share of notional (analysis/whale_test.py)

The deliverable is the filter table: skipping trades must lose LESS total P&L than the
trades it discards, or it is just trading smaller.

  python3 analysis/backstop_filter.py [tape_glob]
"""
import bisect, csv, glob, gzip, math, os, sys
from collections import defaultdict, deque

CANDLES = "hyperliquid_15m_allperps.csv"
UNIVERSE = "perp_universe.csv"
FUNDING = "hyperliquid_funding.csv"
TAPE_GLOB = sys.argv[1] if len(sys.argv) > 1 else "tape/tape_*.csv*"
WIN = 96                      # 24h at 15m
VOL_MULT = 5.0
RV_PCTILE = 0.60
BACKSTOP = 32                 # 8h
COST_BPS = 3.0
MINBARS = 1500


def median(xs):
    s = sorted(xs); n = len(s)
    return s[n//2] if n % 2 else 0.5*(s[n//2-1]+s[n//2])


# ---- tiers ----
vols = {}
for r in csv.DictReader(open(UNIVERSE)):
    try: vols[r["name"]] = float(r["day_notional_vol"])
    except ValueError: pass
sv = sorted(vols.values())
q1, q2 = sv[len(sv)//3], sv[2*len(sv)//3]
tier_of = lambda v: "LOW" if v < q1 else ("MID" if v < q2 else "HIGH")

# ---- funding series ----
fund = defaultdict(list)
for r in csv.DictReader(open(FUNDING)):
    try: fund[r["symbol"]].append((int(r["time_ms"]), float(r["funding_rate"])))
    except ValueError: pass
for s in fund:
    fund[s].sort()

# ---- candles per symbol ----
bysym = defaultdict(list)
for r in csv.DictReader(open(CANDLES)):
    try:
        bysym[r["symbol"]].append((int(r["open_time_ms"]), float(r["high"]),
                                   float(r["low"]), float(r["close"]),
                                   float(r["volume"]), float(r["num_trades"])))
    except ValueError:
        pass
print(f"candles: {len(bysym)} symbols")

# ---- build events, simulate the arm's real exits ----
ev = []
for sym, rows in bysym.items():
    if len(rows) < MINBARS or tier_of(vols.get(sym, 0)) not in ("HIGH", "MID"):
        continue
    rows.sort()
    t = [x[0] for x in rows]; hi = [x[1] for x in rows]; lo = [x[2] for x in rows]
    cl = [x[3] for x in rows]; vo = [x[4] for x in rows]; nt = [x[5] for x in rows]
    aps = [(vo[i]/nt[i] if nt[i] else 0.0) for i in range(len(rows))]
    fs = fund.get(sym)
    ftimes = [x[0] for x in fs] if fs else None
    for i in range(WIN, len(rows) - BACKSTOP - 1):
        med = median(vo[i-WIN:i])
        if med <= 0 or vo[i]/med < VOL_MULT:
            continue
        ph, pl = max(hi[i-WIN:i]), min(lo[i-WIN:i])
        brk = 1 if cl[i] > ph else (-1 if cl[i] < pl else 0)
        if brk == 0:
            continue
        rets = [math.log(cl[j]/cl[j-1]) for j in range(i-WIN+1, i+1)]
        mu = sum(rets)/len(rets)
        rv = (sum((x-mu)**2 for x in rets)/len(rets))**0.5
        # funding gate
        if not ftimes:
            continue
        j = bisect.bisect_right(ftimes, t[i]) - 1
        if j < 0 or (1 if fs[j][1] > 0 else (-1 if fs[j][1] < 0 else 0)) != brk:
            continue
        d = -brk
        entry = cl[i]
        pierce = (cl[i]-ph)/ph if brk == 1 else (pl-cl[i])/pl
        rng = (ph-pl)/entry if entry > 0 else 0.0
        ma = median(aps[i-WIN:i])
        # simulate: reclaim on close, else 8h backstop
        why, ret = "backstop", None
        for k in range(1, BACKSTOP+1):
            c = cl[i+k]
            if (d < 0 and c < ph) or (d > 0 and c > pl):
                why, ret = "reclaim", d*(c-entry)/entry
                break
        if ret is None:
            ret = d*(cl[i+BACKSTOP]-entry)/entry
        ev.append(dict(sym=sym, t=t[i], rv=rv, vratio=vo[i]/med, brk=brk,
                       pierce_bps=pierce*1e4, pierce_frac=(pierce/rng if rng > 0 else 0.0),
                       range_bps=rng*1e4,
                       ats_candle=(aps[i]/ma if ma > 0 else 1.0),
                       funding_abs=abs(fs[j][1])*1e6,
                       why=why, ret_bps=ret*1e4))
thr = sorted(e["rv"] for e in ev)[int(RV_PCTILE*len(ev))]
ev = [e for e in ev if e["rv"] >= thr]
nb = sum(1 for e in ev if e["why"] == "backstop")
print(f"events with full gate set: {len(ev):,}  "
      f"({nb} backstop = {nb/len(ev)*100:.0f}%, {len(ev)-nb} reclaim)")
r_all = [e["ret_bps"] - COST_BPS for e in ev]


def st(xs):
    n = len(xs)
    if n < 2: return (float('nan'), float('nan'), n)
    m = sum(xs)/n
    sd = (sum((x-m)**2 for x in xs)/(n-1))**0.5
    return (m, m/(sd/math.sqrt(n)) if sd > 0 else float('nan'), n)


m0, t0, _ = st(r_all)
print(f"baseline: {m0:+.1f}bps/trade net, t={t0:+.1f}, total {sum(r_all):+,.0f}bps\n")


def quintiles(pool, key, label):
    pts = sorted(pool, key=lambda e: e[key])
    k = len(pts)//5
    if k < 10:
        return
    print(f"  {label}")
    print(f"    {'q':>3} {'range':>19} {'backstop%':>10} {'net bps':>9} {'t':>6}")
    for i in range(5):
        seg = pts[i*k:(i+1)*k] if i < 4 else pts[4*k:]
        br = sum(1 for e in seg if e["why"] == "backstop")/len(seg)*100
        m, t, n = st([e["ret_bps"]-COST_BPS for e in seg])
        print(f"    {i+1:>3} {seg[0][key]:>9.2f}..{seg[-1][key]:>8.2f} {br:>9.0f}% "
              f"{m:>+9.1f} {t:>+6.1f}")
    print()


print("=== CANDLE features, full 52-day sample ===")
for k, lab in (("pierce_bps", "pierce beyond the prior range (bps)"),
               ("pierce_frac", "pierce as a fraction of range width"),
               ("range_bps", "prior 24h range width (bps)"),
               ("vratio", "volume spike multiple"),
               ("rv", "realized vol"),
               ("ats_candle", "avg-trade-size ratio (candle proxy)"),
               ("funding_abs", "|funding| (ppm)")):
    quintiles(ev, k, lab)

print("=== FILTER: skip the worst quintile of each ===")
print(f"  {'rule':>36} {'trades':>7} {'backstop%':>10} {'total bps':>11} "
      f"{'bps/trade':>10} {'t':>7}")
print(f"  {'none':>36} {len(ev):>7} {nb/len(ev)*100:>9.0f}% {sum(r_all):>+11,.0f} "
      f"{m0:>+10.1f} {t0:>+7.1f}")
for k in ("pierce_bps", "pierce_frac", "range_bps", "vratio", "rv", "ats_candle"):
    for frac, lab in ((0.8, "drop top 20%"), (0.8, "drop bottom 20%")):
        pts = sorted(ev, key=lambda e: e[k])
        keep = pts[:int(len(pts)*frac)] if lab == "drop top 20%" else pts[int(len(pts)*0.2):]
        r = [e["ret_bps"]-COST_BPS for e in keep]
        b = sum(1 for e in keep if e["why"] == "backstop")/len(keep)*100
        m, t, n = st(r)
        print(f"  {k + ' ' + lab:>36} {n:>7} {b:>9.0f}% {sum(r):>+11,.0f} "
              f"{m:>+10.1f} {t:>+7.1f}")

# ---- tape features on the covered subset ----
print("\n=== TAPE features (only the days the tape logger covers) ===")
m1 = defaultdict(lambda: [0.0, 0.0, 0.0, 0])       # buy, sell, ntl, n
samp, seen = defaultdict(list), defaultdict(int)
import random
random.seed(11)
tmin = tmax = None
for tf in sorted(glob.glob(TAPE_GLOB)):
    op = gzip.open if tf.endswith(".gz") else open
    try:
        with op(tf, "rt") as f:
            for r in csv.reader(f):
                if not r or r[0] == "time_ms" or len(r) < 5:
                    continue
                try:
                    tt = int(r[0]); sym = r[1]; side = r[2]
                    px = float(r[3]); sz = float(r[4])
                except ValueError:
                    continue
                tmin = tt if tmin is None else min(tmin, tt)
                tmax = tt if tmax is None else max(tmax, tt)
                n = px*sz
                b = m1[(sym, tt//60000)]
                if side == "B": b[0] += n
                else:           b[1] += n
                b[2] += n; b[3] += 1
                s = samp[sym]; seen[sym] += 1
                if len(s) < 20000: s.append(n)
                else:
                    j = random.randint(0, seen[sym]-1)
                    if j < 20000: s[j] = n
    except Exception as e:
        print(f"  WARN {tf}: {e}")
if tmin is None:
    print("  no tape found"); sys.exit(0)
cut = {}
for s, v in samp.items():
    if len(v) >= 200:
        v.sort(); cut[s] = v[int(0.8*len(v))]

sub = []
for e in ev:
    bar_end = e["t"] + 15*60*1000                  # the signal bar closes here
    if not (tmin <= e["t"] and bar_end <= tmax):
        continue
    em = bar_end // 60000
    num = den = signed = 0.0
    seenm = 0
    for mm in range(em-60, em):
        v = m1.get((e["sym"], mm))
        if not v:
            continue
        num += abs(v[0]-v[1]); den += v[0]+v[1]; signed += v[0]-v[1]; seenm += 1
    if den <= 0 or seenm < 10:
        continue
    # adverse_ofi: flow continuing in the breakout direction is flow running into our
    # fade, so sign the trailing imbalance by brk (stored at event build time)
    sub.append(dict(e, vpin60=num/den, adverse_ofi=(signed/den)*e["brk"]))
print(f"  events inside tape coverage: {len(sub)} of {len(ev)}")
if len(sub) >= 60:
    nbs = sum(1 for e in sub if e["why"] == "backstop")
    ms, ts, _ = st([e["ret_bps"]-COST_BPS for e in sub])
    print(f"  subset baseline: {ms:+.1f}bps/trade, t={ts:+.1f}, "
          f"backstop {nbs/len(sub)*100:.0f}%\n")
    for k, lab in (("vpin60", "flow toxicity (vpin60)"),
                   ("adverse_ofi", "flow running INTO the fade (signed by breakout dir)")):
        quintiles(sub, k, lab)
else:
    print(f"  too few ({len(sub)}) -- the tape covers ~5 days against a 52-day event set,")
    print("  so this only becomes answerable as the logger accumulates history.")
