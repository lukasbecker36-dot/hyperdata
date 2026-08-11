#!/usr/bin/env python3
"""Extract the raw prints INSIDE each spike bar, at sub-minute resolution and by size band.

flow_tilt.py closed the intra-spike question at 1-minute granularity with a $10k big-print
threshold, and left two things genuinely untested rather than refuted:

  1. SIGNED BIG-PRINT FLOW had only 255 usable gated observations, because most spike bars
     contain no $10k+ print at all. That is a coverage failure, not a null result, and it
     is the feature closest to "is a whale still leaning on this".
  2. SUB-MINUTE STRUCTURE was invisible. Everything was 1m buckets inside a 15m bar. If
     exhaustion resolves in seconds -- the aggressor lifting the last offer and stopping --
     nothing at 1m could see it.

Rebuilding all 36M prints at 10s with several size bands would be enormous and mostly
wasted: only the 4,833 spike windows matter, which is ~1.2% of the tape. So this streams
the tape ONCE and keeps only prints falling inside a spike window, bucketed at 30s and
split by size band so ANY big-print threshold can be reconstructed afterwards without
another pass.

Per (event, 30s bucket):
  n                  print count
  b0..b3 / s0..s3    BUY and SELL notional in size bands
                     [0,1k) [1k,5k) [5k,20k) [20k,inf)
  bmax, smax         largest single buy / sell print, so "one whale" is separable from
                     "many mediums" at any cutoff

Windows are keyed by bar OPEN and a print at exactly t belongs to the bar starting at t,
matching the point-in-time convention in fc_bars.py.

  python3 analysis/spike_tape.py /opt/hyperdata/tape events.csv out.csv.gz
"""
import bisect, glob, gzip, os, sys

TAPE = sys.argv[1] if len(sys.argv) > 1 else "/opt/hyperdata/tape"
EVENTS = sys.argv[2] if len(sys.argv) > 2 else "events.csv"
OUT = sys.argv[3] if len(sys.argv) > 3 else "spike_tape.csv.gz"
BAR_MS = 900000
SUB_MS = 30000                      # 30s -> 30 buckets per 15m bar
EDGES = (1000.0, 5000.0, 20000.0)   # size-band boundaries in USD notional

# ---- load event windows, grouped by coin ----
wins = {}
with open(EVENTS) as f:
    hdr = f.readline().rstrip("\n").split(",")
    ci, ti = hdr.index("sym"), hdr.index("t")
    for line in f:
        p = line.rstrip("\n").split(",")
        wins.setdefault(p[ci], []).append(int(p[ti]))
for c in wins:
    wins[c].sort()
idx = {c: (v, {t: i for i, t in enumerate(v)}) for c, v in wins.items()}
n_ev = sum(len(v) for v in wins.values())
print(f"{n_ev:,} event windows across {len(wins)} coins")

files = sorted(glob.glob(os.path.join(TAPE, "tape_*.csv.gz")) +
               glob.glob(os.path.join(TAPE, "tape_*.csv")))
print(f"{len(files)} tape files")

agg = {}          # (coin, win_start, sub) -> [n, b0..b3, s0..s3, bmax, smax]
kept = total = 0
for path in files:
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", errors="replace") as f:
        f.readline()
        for line in f:
            p = line.split(",")
            if len(p) < 5:
                continue
            coin = p[1]
            w = idx.get(coin)
            if w is None:
                continue
            try:
                t = int(p[0]); px = float(p[3]); sz = float(p[4])
            except ValueError:
                continue
            total += 1
            starts = w[0]
            # a print can only fall in a window that starts at or before it; check the
            # two most recent candidates so overlapping signals on one coin are not lost
            j = bisect.bisect_right(starts, t) - 1
            hit = None
            for k in (j, j - 1):
                if 0 <= k < len(starts) and starts[k] <= t < starts[k] + BAR_MS:
                    hit = starts[k]
                    break
            if hit is None:
                continue
            ntl = px * sz
            band = 0 if ntl < EDGES[0] else (1 if ntl < EDGES[1] else
                                             (2 if ntl < EDGES[2] else 3))
            key = (coin, hit, (t - hit) // SUB_MS)
            a = agg.get(key)
            if a is None:
                a = agg[key] = [0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
            a[0] += 1
            if p[2] == "B":
                a[1 + band] += ntl
                if ntl > a[9]:
                    a[9] = ntl
            else:
                a[5 + band] += ntl
                if ntl > a[10]:
                    a[10] = ntl
            kept += 1
    print(f"  {os.path.basename(path):<28} kept {kept:>9,} of {total:>10,}")

out = gzip.open(OUT, "wt", newline="")
out.write("coin,win_ms,sub,n,b0,b1,b2,b3,s0,s1,s2,s3,bmax,smax\n")
for (c, w, s), a in sorted(agg.items()):
    out.write(f"{c},{w},{s},{a[0]},"
              + ",".join(f"{x:.2f}" for x in a[1:9])
              + f",{a[9]:.2f},{a[10]:.2f}\n")
out.close()
print(f"\nkept {kept:,} of {total:,} prints ({100*kept/max(1,total):.1f}%) "
      f"-> {len(agg):,} rows -> {OUT} ({os.path.getsize(OUT)/1e6:.1f} MB)")
