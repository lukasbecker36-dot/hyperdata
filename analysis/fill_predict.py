#!/usr/bin/env python3
"""Does queue position predict whether we fill? The loop-closer.

taker_now.py established the prize: maker the signals that will fill, cross the ones that
will not, worth +$92.55 against +$59.39 for today's abandon-everything policy -- a 56%
improvement, the largest number in this investigation. It needs one input we never had:
a prediction, at placement time, of whether the order will fill.

The live bot now records that input. Ported from provision_bot.py / mexc_api.py:
    queue_usd    USD resting at or better than our price when we placed
    queue_ratio  that, divided by our own order size

This script closes the loop three ways.

1. FILL RATE BY QUEUE DEPTH. The direct test. If queue_ratio predicts fills, a shallow
   queue should fill far more often than a deep one.

2. THE QUEUE VERDICT, reconstructed from the tape. provision_bot.py polls the trade feed
   while its order rests to accumulate queue-consuming volume. We do not need to: the tape
   logger already captured every print, so consumption is recoverable offline at zero API
   cost. For each order, sum opposing-aggressor notional at or through our level during
   the rest window, then apply the original rule:
        queue_fill = consumed >= queue_ahead + our_size
   Comparing that to the REAL fill is the first honest validation of the queue model --
   and unlike every offline fill study here so far, the label is a real exchange fill, not
   a proxy that turned out endogenous (see the note in analysis/exhaustion_sweep.py).

3. WHAT THE PREDICTION IS WORTH. Split trades by predicted fill and price the mixed
   policy: rest when we expect to fill, cross immediately when we do not.

  python3 analysis/fill_predict.py [datadir] [tape_glob]
"""
import bisect, csv, glob, gzip, math, os, sys
from collections import defaultdict
from datetime import datetime, timezone

DATADIR = sys.argv[1] if len(sys.argv) > 1 else "live_15m_ats"
TAPE_GLOB = sys.argv[2] if len(sys.argv) > 2 else "tape/tape_*.csv*"
ENTRY_WIN_S = 300
MAKER_BPS, TAKER_BPS = 1.5, 4.5


def pms(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def fnum(x):
    try:
        return float(x) if x not in (None, "") else None
    except ValueError:
        return None


# ---- orders: fills from the trade log, misses from the miss log ----
orders = []
tp = os.path.join(DATADIR, "trades_15m.csv")
mp = os.path.join(DATADIR, "missed_15m_ats.csv")
if os.path.exists(tp):
    for r in csv.DictReader(open(tp)):
        q = fnum(r.get("queue_usd"))
        if q is None:
            continue                      # placed before queue logging existed
        wait = fnum(r.get("entry_wait_s")) or 0.0
        net = fnum(r.get("net_bps"))
        orders.append(dict(
            sym=r["symbol"], side=r["side"], filled=True,
            level=float(r["entry_px"]), sz=fnum(r.get("sz")) or 0.0,
            queue_usd=q, queue_ratio=fnum(r.get("queue_ratio")),
            spread_bps=fnum(r.get("spread_bps")),
            vpin60=fnum(r.get("vpin60")), adverse_ofi=fnum(r.get("adverse_ofi")),
            net_bps=net,
            t0=pms(r["entry_time"]) - int(wait * 1000), win_s=wait or ENTRY_WIN_S))
if os.path.exists(mp):
    for r in csv.DictReader(open(mp)):
        q = fnum(r.get("queue_usd"))
        if q is None:
            continue
        orders.append(dict(
            sym=r["symbol"], side=r["side"], filled=False,
            level=float(r["px"]), sz=fnum(r.get("sz")) or 0.0,
            queue_usd=q, queue_ratio=fnum(r.get("queue_ratio")),
            spread_bps=fnum(r.get("spread_bps")),
            vpin60=fnum(r.get("vpin60")), adverse_ofi=fnum(r.get("adverse_ofi")),
            net_bps=None,
            t0=pms(r["time"]) - int((fnum(r.get("rested_s")) or ENTRY_WIN_S) * 1000),
            win_s=fnum(r.get("rested_s")) or ENTRY_WIN_S))

nf = sum(1 for o in orders if o["filled"])
print(f"{DATADIR}: {len(orders)} orders with queue data "
      f"({nf} filled, {len(orders)-nf} missed)")
if not orders:
    print("\nNothing to analyse yet -- queue logging is new, so only orders placed from")
    print("now on carry it. Re-run once the live arm has accumulated signals.")
    sys.exit(0)


def st(xs):
    n = len(xs)
    if n < 2: return (float('nan'), float('nan'), n)
    m = sum(xs)/n
    sd = (sum((x-m)**2 for x in xs)/(n-1))**0.5
    return (m, m/(sd/math.sqrt(n)) if sd > 0 else float('nan'), n)


# ---- 1. fill rate by queue depth ----
print("\n=== 1. fill rate by queue depth ===")
qs = [o for o in orders if o["queue_ratio"] is not None]
if len(qs) >= 8:
    qs.sort(key=lambda o: o["queue_ratio"])
    nb = 4 if len(qs) >= 16 else 2
    k = len(qs)//nb
    print(f"  {'bucket':>8} {'queue_ratio':>18} {'n':>4} {'fill%':>7}")
    for i in range(nb):
        seg = qs[i*k:(i+1)*k] if i < nb-1 else qs[(nb-1)*k:]
        fr = sum(1 for o in seg if o["filled"])/len(seg)*100
        print(f"  {i+1:>8} {seg[0]['queue_ratio']:>8.1f}..{seg[-1]['queue_ratio']:>8.1f} "
              f"{len(seg):>4} {fr:>6.0f}%")
else:
    print(f"  only {len(qs)} orders -- need ~8+ for buckets")
    for o in qs:
        print(f"    {o['sym']:>10} {'FILL' if o['filled'] else 'MISS'} "
              f"queue=${o['queue_usd']:,.0f} = {o['queue_ratio']:.1f}x ours"
              f"  spread={o['spread_bps']:.1f}b")

# ---- 2. the queue verdict, reconstructed from the tape ----
print("\n=== 2. queue verdict from the tape (provision_bot's rule, offline) ===")
wins = defaultdict(list)
for i, o in enumerate(orders):
    wins[o["sym"]].append((o["t0"], o["t0"] + int(o["win_s"] * 1000), i))
consumed = [0.0] * len(orders)
starts, ends, idxs = {}, {}, {}
for c, ws in wins.items():
    ws.sort()
    starts[c] = [w[0] for w in ws]; ends[c] = [w[1] for w in ws]; idxs[c] = [w[2] for w in ws]
seen_tape = False
for tf in sorted(glob.glob(TAPE_GLOB)):
    op = gzip.open if tf.endswith(".gz") else open
    try:
        with op(tf, "rt") as f:
            for r in csv.reader(f):
                if not r or r[0] == "time_ms" or len(r) < 5:
                    continue
                try:
                    t = int(r[0]); sym = r[1]; side = r[2]
                    px = float(r[3]); sz = float(r[4])
                except ValueError:
                    continue
                ss = starts.get(sym)
                if not ss:
                    continue
                seen_tape = True
                j = bisect.bisect_right(ss, t) - 1
                for jj in (j, j-1):
                    if jj < 0 or jj >= len(ss) or t < ss[jj] or t > ends[sym][jj]:
                        continue
                    i = idxs[sym][jj]
                    o = orders[i]
                    # a resting SELL is consumed by BUY-aggressors at/through our level
                    if o["side"] == "SHORT" and side == "B" and px >= o["level"]:
                        consumed[i] += px * sz
                    elif o["side"] == "LONG" and side == "A" and px <= o["level"]:
                        consumed[i] += px * sz
    except Exception as e:
        print(f"  WARN {tf}: {e}")
if not seen_tape:
    print("  no tape rows matched these orders (tape may not cover them yet)")
else:
    agree = both = 0
    print(f"  {'sym':>10} {'real':>6} {'queue rule':>11} {'queue$':>10} "
          f"{'consumed$':>11} {'need$':>10}")
    for i, o in enumerate(orders):
        need = o["queue_usd"] + o["sz"] * o["level"]
        pred = consumed[i] >= need
        both += 1
        agree += 1 if pred == o["filled"] else 0
        print(f"  {o['sym']:>10} {'FILL' if o['filled'] else 'MISS':>6} "
              f"{'FILL' if pred else 'MISS':>11} {o['queue_usd']:>10,.0f} "
              f"{consumed[i]:>11,.0f} {need:>10,.0f}")
    if both:
        print(f"\n  queue rule agreed with the real fill on {agree}/{both} "
              f"({agree/both*100:.0f}%)")
        print("  (this is the validation no earlier fill study could do: the label here is")
        print("   a real exchange fill, not a proxy correlated with the outcome)")

# ---- 3. what the prediction would be worth ----
print("\n=== 3. value of predicting the fill ===")
fills = [o for o in orders if o["filled"] and o["net_bps"] is not None]
if len(fills) >= 5:
    m, t, n = st([o["net_bps"] for o in fills])
    print(f"  filled trades: n={n}  mean {m:+.1f}bps  t={t:+.1f}")
    print(f"  taker_now.py measured the missed signals at +$0.313/trade net of spread")
    print(f"  and taker fee (t=+2.7), so each correctly-predicted miss is worth roughly")
    print(f"  that much -- provided the prediction is right.")
else:
    print(f"  only {len(fills)} filled trades with P&L -- too few to price yet")
print()
print("Re-run this as the live arm accumulates. The queue rule's agreement rate in")
print("section 2 is the number that decides whether the +$92.55 policy is reachable.")
