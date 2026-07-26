#!/usr/bin/env python3
"""Can we predict a TOXIC fill before placing the order?

The problem, already measured rather than hypothesised. The shadow-fill audit showed:
  - only ~76% of maker entries ever fill;
  - filtering to filled-only cut booked P&L from +$111 to +$59, so the fills we WIN are
    systematically worse than the ones we miss;
  - the never-filled trades were worth ~+$0.87/trade -- alpha that is unreachable,
    because by the time you know you missed, price has moved ~35bps away.

That is adverse selection: a resting sell only gets lifted while buyers keep coming, so
you are filled precisely when the fade has NOT started. You cannot recover the misses
(taker_entry.py proved chasing loses). But you can try to avoid the bad fills.

So the question is not "will this fill" -- an unfilled order costs nothing. It is
"conditional on filling, will this fill be a loser", answerable BEFORE placing.

Features, all computed from tape strictly BEFORE the signal bar closes:
  vpin30/vpin60  time-bar VPIN: sum|buy-sell| / sum(buy+sell) over the trailing 30/60m.
                 Unsigned flow toxicity. High = one-sided, informed-looking flow.
  adverse_ofi    signed toxicity: trailing OFI x breakout direction. We fade, so flow
                 continuing in the breakout direction is flow running INTO our order.
                 This is the sharpest statement of "am I about to be run over".
  intensity      prints in the signal bar vs its trailing median. Is this a burst?

Outcomes, both from the tape:
  filled   would a resting order at the bar's closing price have been printed THROUGH
           within ENTRY_WINDOW_S? Same rule as shadow_fill2.py.
  pnl_bps  the fade return over HOLD bars, matching the strategy's 8h backstop.

The deliverable is the last table: if we skip the most toxic bucket, what happens to
total P&L and to the number of trades? A filter that removes 20% of trades must keep
more than 20% of the P&L to be worth having.

  python3 analysis/toxicity.py [vol_mult]
"""
import csv, gzip, glob, math, sys
from collections import defaultdict, deque

TAPE_GLOB   = "tape/tape_*.csv*"
VOL_MULT    = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
BAR_MS      = 60 * 1000              # 1m feature bars
PER_15      = 15                     # 1m bars per signal bar
TRAIL_15    = 96                     # 24h of 15m bars, for range + volume median
ENTRY_WIN_S = 300
HOLD_15     = 32                     # 8h in 15m bars
MIN_TRADES  = 20
COST_BPS    = 3.0


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


# ---- pass 1: 1m bars ----
# [buy, sell, n, first, last, hi, lo]
m1 = defaultdict(lambda: [0.0, 0.0, 0, 0.0, 0.0, 0.0, 1e30])
for t, coin, side, px, sz in prints():
    b = m1[(coin, t // BAR_MS)]
    ntl = px * sz
    if side == "B": b[0] += ntl
    else:           b[1] += ntl
    b[2] += 1
    if b[3] == 0.0: b[3] = px
    b[4] = px
    if px > b[5]: b[5] = px
    if px < b[6]: b[6] = px
print(f"pass 1: {len(m1):,} coin-minutes")

by_coin = defaultdict(dict)
for (coin, bi), v in m1.items():
    by_coin[coin][bi] = v


def median(xs):
    s = sorted(xs); n = len(s)
    return s[n//2] if n % 2 else 0.5*(s[n//2-1]+s[n//2])


# ---- build 15m signal bars from the 1m bars, then find events ----
events = []
for coin, d in by_coin.items():
    if not d:
        continue
    lo_i, hi_i = min(d), max(d)
    g = {}                                  # 15m index -> aggregated
    for bi in range(lo_i, hi_i + 1):
        v = d.get(bi)
        if not v:
            continue
        k = bi // PER_15
        a = g.setdefault(k, [0.0, 0.0, 0, 0.0, 0.0, 0.0, 1e30])
        a[0] += v[0]; a[1] += v[1]; a[2] += v[2]
        if a[3] == 0.0: a[3] = v[3]
        a[4] = v[4]
        a[5] = max(a[5], v[5]); a[6] = min(a[6], v[6])
    ks = sorted(g)
    h_ntl, h_hi, h_lo, h_n = deque(maxlen=TRAIL_15), deque(maxlen=TRAIL_15), \
                             deque(maxlen=TRAIL_15), deque(maxlen=TRAIL_15)
    for k in ks:
        a = g[k]
        ntl = a[0] + a[1]
        if len(h_ntl) >= TRAIL_15 // 2 and a[2] >= MIN_TRADES and a[4] > 0:
            mn = median(h_ntl)
            ph, pl = max(h_hi), min(h_lo)
            mnn = median(h_n) or 1
            if mn > 0:
                vr = ntl / mn
                brk = 1 if a[4] > ph else (-1 if a[4] < pl else 0)
                if vr >= VOL_MULT and brk != 0:
                    # --- features from 1m bars strictly BEFORE this signal bar closes ---
                    end = k * PER_15 + PER_15 - 1          # last 1m bar of the signal bar
                    def win(nmin):
                        out = []
                        for j in range(end - nmin + 1, end + 1):
                            vv = d.get(j)
                            if vv: out.append(vv)
                        return out
                    def vpin(nmin):
                        w = win(nmin)
                        num = sum(abs(x[0] - x[1]) for x in w)
                        den = sum(x[0] + x[1] for x in w)
                        return num / den if den > 0 else None
                    w60 = win(60)
                    tot60 = sum(x[0] + x[1] for x in w60)
                    ofi60 = (sum(x[0] for x in w60) - sum(x[1] for x in w60)) / tot60 if tot60 > 0 else 0.0
                    v30, v60 = vpin(30), vpin(60)
                    if v30 is not None and v60 is not None:
                        # forward fade return
                        nx = g.get(k + HOLD_15)
                        fwd = (-brk) * (nx[4] / a[4] - 1.0) * 1e4 if (nx and nx[4] > 0) else None
                        if fwd is not None:
                            events.append(dict(
                                coin=coin, t0=(k * PER_15 + PER_15) * BAR_MS,
                                level=a[4], brk=brk, vr=vr, fwd=fwd,
                                vpin30=v30, vpin60=v60,
                                adverse_ofi=ofi60 * brk,
                                intensity=a[2] / (mnn * 1.0)))
        h_ntl.append(ntl); h_hi.append(a[5]); h_lo.append(a[6]); h_n.append(a[2])
print(f"events (vratio>={VOL_MULT}, with features + 8h outcome): {len(events):,}")
if len(events) < 300:
    print("too few events"); sys.exit(0)

# ---- pass 2: prints inside each event's fill window, to decide `filled` ----
wins = defaultdict(list)
for i, e in enumerate(events):
    wins[e["coin"]].append((e["t0"], e["t0"] + ENTRY_WIN_S * 1000, i))
starts, ends, idxs = {}, {}, {}
for c, ws in wins.items():
    ws.sort()
    starts[c] = [w[0] for w in ws]; ends[c] = [w[1] for w in ws]; idxs[c] = [w[2] for w in ws]
import bisect
hit = [False] * len(events)
for t, coin, side, px, sz in prints():
    ss = starts.get(coin)
    if not ss:
        continue
    j = bisect.bisect_right(ss, t) - 1
    # an event window may overlap its neighbour; check a couple back
    for jj in (j, j - 1):
        if jj < 0 or jj >= len(ss):
            continue
        if t < ss[jj] or t > ends[coin][jj]:
            continue
        i = idxs[coin][jj]
        if hit[i]:
            continue
        e = events[i]
        # short entry (brk=+1) rests a SELL -> needs a BUY-aggressor print ABOVE it
        # long  entry (brk=-1) rests a BUY  -> needs a SELL-aggressor print BELOW it
        if e["brk"] == 1 and side == "B" and px > e["level"]:
            hit[i] = True
        elif e["brk"] == -1 and side == "A" and px < e["level"]:
            hit[i] = True
for i, e in enumerate(events):
    e["filled"] = hit[i]
nf = sum(1 for e in events if e["filled"])
print(f"pass 2: fill rate {nf}/{len(events)} = {nf/len(events)*100:.0f}%"
      f"   (audit on real orders: ~76%)\n")


def st(xs):
    n = len(xs)
    if n < 2: return (float('nan'), float('nan'), n)
    m = sum(xs)/n
    sd = (sum((x-m)**2 for x in xs)/(n-1))**0.5
    return (m, m/(sd/math.sqrt(n)) if sd > 0 else float('nan'), n)


print("=== does toxicity predict being FILLED? (adverse selection's first signature) ===")
for key in ("vpin30", "vpin60", "adverse_ofi", "intensity"):
    pts = sorted(events, key=lambda e: e[key])
    k = len(pts)//5
    fr = []
    for i in range(5):
        seg = pts[i*k:(i+1)*k] if i < 4 else pts[4*k:]
        fr.append(sum(1 for e in seg if e["filled"])/len(seg)*100)
    print(f"  {key:>12} fill% by quintile: " + "  ".join(f"{x:5.1f}" for x in fr)
          + f"   (top-bottom {fr[-1]-fr[0]:+.1f}pp)")

print("\n=== conditional on FILLING, does toxicity predict a WORSE trade? ===")
print("    (this is the one that matters -- it is the P&L of trades we actually get)")
filled = [e for e in events if e["filled"]]
print(f"    n filled = {len(filled)}")
for key in ("vpin30", "vpin60", "adverse_ofi", "intensity"):
    pts = sorted(filled, key=lambda e: e[key])
    k = len(pts)//5
    print(f"\n  {key}")
    print(f"    {'q':>3} {'range':>16} {'net bps':>9} {'t':>6} {'win%':>6}")
    for i in range(5):
        seg = pts[i*k:(i+1)*k] if i < 4 else pts[4*k:]
        r = [e["fwd"] - COST_BPS for e in seg]
        m, t, n = st(r)
        print(f"    {i+1:>3} {seg[0][key]:>7.3f}..{seg[-1][key]:>6.3f} {m:>+9.2f} {t:>+6.1f} "
              f"{sum(1 for x in r if x>0)/len(r)*100:>5.0f}%")

print("\n=== THE FILTER: skip the most toxic bucket, what happens? ===")
print(f"  {'filter':>28} {'trades kept':>12} {'total net bps':>14} {'bps/trade':>10} {'t':>7}")
base = [e["fwd"] - COST_BPS for e in filled]
bm, bt, bn = st(base)
print(f"  {'none (all filled trades)':>28} {bn:>12} {sum(base):>+14.0f} {bm:>+10.2f} {bt:>+7.1f}")
for key in ("vpin30", "vpin60", "adverse_ofi", "intensity"):
    for frac, lab in ((0.8, "drop top 20%"), (0.6, "drop top 40%")):
        pts = sorted(filled, key=lambda e: e[key])
        keep = pts[:int(len(pts)*frac)]
        r = [e["fwd"] - COST_BPS for e in keep]
        m, t, n = st(r)
        print(f"  {key + ' ' + lab:>28} {n:>12} {sum(r):>+14.0f} {m:>+10.2f} {t:>+7.1f}")
print()
print("A filter is only worth it if total P&L falls by LESS than the trade count does --")
print("otherwise you are just trading smaller. Compare 'total net bps' against the")
print("no-filter row, not 'bps/trade', which improves trivially by dropping trades.")
