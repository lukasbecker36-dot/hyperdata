#!/usr/bin/env python3
"""Two tape-only conditioners for the fade: EXHAUSTION and SWEEPS.

Both are invisible to candles, both are cheap, and they should point OPPOSITE ways --
which is what makes them a useful pair rather than two guesses.

EXHAUSTION FLIP
  Within the signal bar, did the aggressor side that drove the move fade or reverse by
  the end? Buyers early and sellers late on an up-move means the push ran out of fuel.
      exhaustion = brk * (ofi_early - ofi_late)
  computed from the bar's constituent 1-minute buckets (first third vs last third), with
  brk the breakout direction so the sign means the same thing for up and down breaks.
  HIGH exhaustion should mean the fade works BETTER.

SWEEPS
  One aggressor tearing through several price levels in milliseconds. That is urgency,
  usually informed, and it should mean the move CONTINUES -- so the fade works WORSE.
  A sweep here is a run of same-side aggressor prints, each within GAP_MS of the last,
  moving monotonically in the aggressor's direction, touching >= MIN_LEVELS distinct
  prices inside MAX_SPAN_MS.
      sweep_share     = sweep notional / bar notional
      sweep_dir_share = sweep notional IN THE BREAKOUT DIRECTION / bar notional

If both work, the combination is a filter: fade exhausted spikes, skip swept ones. The
last two tables test exactly that, and are the only ones that matter -- a conditioner
that improves bps/trade by throwing away trades has done nothing.

  python3 analysis/exhaustion_sweep.py [vol_mult]
"""
import csv, gzip, glob, math, sys
from collections import defaultdict, deque

TAPE_GLOB   = "tape/tape_*.csv*"
VOL_MULT    = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
BAR_MS      = 60 * 1000
PER_15      = 15
TRAIL_15    = 96
HOLD_15     = 32                  # 8h, the strategy's backstop
MIN_TRADES  = 20
COST_BPS    = 3.0
# sweep detection
GAP_MS      = 250                 # max gap between prints inside one sweep
MAX_SPAN_MS = 2000                # max total duration of a sweep
MIN_LEVELS  = 3                   # distinct prices consumed


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


# ---- single pass: 1m bars AND sweep detection ----
m1 = defaultdict(lambda: [0.0, 0.0, 0, 0.0, 0.0, 0.0, 1e30])   # buy,sell,n,first,last,hi,lo
sw = defaultdict(lambda: [0.0, 0.0, 0])                        # sweepB, sweepA, count
run = {}      # coin -> dict(side, t0, tl, ntl, levels:set, ext)


def close_run(coin):
    r = run.pop(coin, None)
    if not r:
        return
    if len(r["levels"]) >= MIN_LEVELS and (r["tl"] - r["t0"]) <= MAX_SPAN_MS:
        b = sw[(coin, r["t0"] // BAR_MS)]
        if r["side"] == "B": b[0] += r["ntl"]
        else:               b[1] += r["ntl"]
        b[2] += 1


for t, coin, side, px, sz in prints():
    ntl = px * sz
    b = m1[(coin, t // BAR_MS)]
    if side == "B": b[0] += ntl
    else:           b[1] += ntl
    b[2] += 1
    if b[3] == 0.0: b[3] = px
    b[4] = px
    if px > b[5]: b[5] = px
    if px < b[6]: b[6] = px
    # --- sweep run state ---
    r = run.get(coin)
    ok = (r is not None and r["side"] == side and (t - r["tl"]) <= GAP_MS
          and (px >= r["ext"] if side == "B" else px <= r["ext"]))
    if ok:
        r["tl"] = t; r["ntl"] += ntl; r["levels"].add(px); r["ext"] = px
    else:
        close_run(coin)
        run[coin] = dict(side=side, t0=t, tl=t, ntl=ntl, levels={px}, ext=px)
for c in list(run):
    close_run(c)
print(f"pass: {len(m1):,} coin-minutes, {sum(v[2] for v in sw.values()):,} sweeps detected")

by_coin = defaultdict(dict)
for (coin, bi), v in m1.items():
    by_coin[coin][bi] = v


def median(xs):
    s = sorted(xs); n = len(s)
    return s[n//2] if n % 2 else 0.5*(s[n//2-1]+s[n//2])


def ofi(bars):
    tot = sum(x[0] + x[1] for x in bars)
    return (sum(x[0] for x in bars) - sum(x[1] for x in bars)) / tot if tot > 0 else None


# ---- 15m events with both features ----
ev = []
for coin, d in by_coin.items():
    if not d:
        continue
    g = {}
    for bi, v in d.items():
        k = bi // PER_15
        a = g.setdefault(k, [0.0, 0.0, 0, 0.0, 0.0, 0.0, 1e30])
        a[0] += v[0]; a[1] += v[1]; a[2] += v[2]
        a[4] = v[4]
        a[5] = max(a[5], v[5]); a[6] = min(a[6], v[6])
    for k in g:
        f = d.get(k * PER_15)
        g[k][3] = f[3] if f else g[k][4]
    h_ntl, h_hi, h_lo = deque(maxlen=TRAIL_15), deque(maxlen=TRAIL_15), deque(maxlen=TRAIL_15)
    for k in sorted(g):
        a = g[k]
        ntl = a[0] + a[1]
        if len(h_ntl) >= TRAIL_15 // 2 and a[2] >= MIN_TRADES and a[4] > 0:
            mn = median(h_ntl); ph, pl = max(h_hi), min(h_lo)
            if mn > 0:
                vr = ntl / mn
                brk = 1 if a[4] > ph else (-1 if a[4] < pl else 0)
                if vr >= VOL_MULT and brk != 0:
                    base = k * PER_15
                    early = [d[j] for j in range(base, base + 5) if j in d]
                    late = [d[j] for j in range(base + 10, base + PER_15) if j in d]
                    oe, ol = (ofi(early) if early else None), (ofi(late) if late else None)
                    nx = g.get(k + HOLD_15)
                    fwd = (-brk) * (nx[4] / a[4] - 1.0) * 1e4 if (nx and nx[4] > 0) else None
                    if oe is not None and ol is not None and fwd is not None and ntl > 0:
                        s = [0.0, 0.0, 0]
                        for j in range(base, base + PER_15):
                            q = sw.get((coin, j))
                            if q:
                                s[0] += q[0]; s[1] += q[1]; s[2] += q[2]
                        dir_sw = s[0] if brk == 1 else s[1]
                        ev.append(dict(
                            coin=coin, brk=brk, vr=vr, fwd=fwd,
                            exhaustion=brk * (oe - ol),
                            sweep_share=(s[0] + s[1]) / ntl,
                            sweep_dir_share=dir_sw / ntl,
                            n_sweeps=s[2]))
        h_ntl.append(ntl); h_hi.append(a[5]); h_lo.append(a[6])
print(f"events (vratio>={VOL_MULT}, both features + 8h outcome): {len(ev):,}\n")
if len(ev) < 300:
    print("too few events"); sys.exit(0)


def st(xs):
    n = len(xs)
    if n < 2: return (float('nan'), float('nan'), n)
    m = sum(xs)/n
    sd = (sum((x-m)**2 for x in xs)/(n-1))**0.5
    return (m, m/(sd/math.sqrt(n)) if sd > 0 else float('nan'), n)


def table(key, expect):
    pts = sorted(ev, key=lambda e: e[key])
    k = len(pts)//5
    print(f"  {key}   (expect fade to be {expect} as this rises)")
    print(f"    {'q':>3} {'range':>17} {'net bps':>9} {'t':>6} {'win%':>6} {'coins':>7}")
    out = []
    for i in range(5):
        seg = pts[i*k:(i+1)*k] if i < 4 else pts[4*k:]
        r = [e["fwd"] - COST_BPS for e in seg]
        m, t, n = st(r)
        per = defaultdict(list)
        for e in seg: per[e["coin"]].append(e["fwd"] - COST_BPS)
        sg = [sum(v)/len(v) for v in per.values() if len(v) >= 8]
        ag = (sum(1 for x in sg if x > 0)/len(sg)*100) if sg else float('nan')
        out.append(m)
        print(f"    {i+1:>3} {seg[0][key]:>8.3f}..{seg[-1][key]:>7.3f} {m:>+9.2f} {t:>+6.1f} "
              f"{sum(1 for x in r if x>0)/len(r)*100:>5.0f}% {ag:>6.0f}%")
    print(f"    top minus bottom: {out[-1]-out[0]:+.2f} bps\n")


print("=== univariate ===")
table("exhaustion", "BETTER")
table("sweep_share", "WORSE")
table("sweep_dir_share", "WORSE")

print("=== interaction: exhaustion x sweep (terciles) ===")
ex_s = sorted(e["exhaustion"] for e in ev)
sw_s = sorted(e["sweep_dir_share"] for e in ev)
ex_c = [ex_s[len(ex_s)//3], ex_s[2*len(ex_s)//3]]
sw_c = [sw_s[len(sw_s)//3], sw_s[2*len(sw_s)//3]]
lab = ["low", "mid", "high"]
def ter(v, c): return 0 if v <= c[0] else (1 if v <= c[1] else 2)
print(f"    {'':>12} " + "".join(f"{'sweep ' + l:>14}" for l in lab))
for i in range(3):
    row = f"    {'exh ' + lab[i]:>12} "
    for j in range(3):
        seg = [e for e in ev if ter(e["exhaustion"], ex_c) == i and ter(e["sweep_dir_share"], sw_c) == j]
        if len(seg) < 20:
            row += f"{'-':>14}"; continue
        m, t, n = st([e["fwd"] - COST_BPS for e in seg])
        row += f"{m:>+9.1f}({n:>3})"
    print(row)

print("\n=== THE FILTER: does combining them beat trading everything? ===")
print(f"  {'rule':>34} {'trades':>7} {'total net bps':>14} {'bps/trade':>10} {'t':>7}")
base = [e["fwd"] - COST_BPS for e in ev]
bm, bt, bn = st(base)
print(f"  {'none (all events)':>34} {bn:>7} {sum(base):>+14.0f} {bm:>+10.2f} {bt:>+7.1f}")
rules = [
    ("skip top-tercile sweep_dir", lambda e: ter(e["sweep_dir_share"], sw_c) < 2),
    ("keep top-tercile exhaustion", lambda e: ter(e["exhaustion"], ex_c) == 2),
    ("keep exh>=mid AND sweep<=mid", lambda e: ter(e["exhaustion"], ex_c) >= 1
                                     and ter(e["sweep_dir_share"], sw_c) <= 1),
    ("keep exh high AND sweep low", lambda e: ter(e["exhaustion"], ex_c) == 2
                                    and ter(e["sweep_dir_share"], sw_c) == 0),
]
for name, f in rules:
    seg = [e for e in ev if f(e)]
    if len(seg) < 20:
        continue
    r = [e["fwd"] - COST_BPS for e in seg]
    m, t, n = st(r)
    print(f"  {name:>34} {n:>7} {sum(r):>+14.0f} {m:>+10.2f} {t:>+7.1f}")
print()
print("Compare 'total net bps' with the no-filter row. bps/trade always improves when you")
print("discard trades, so it proves nothing on its own.")
