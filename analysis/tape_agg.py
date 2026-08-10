#!/usr/bin/env python3
"""Reduce the raw tape to per-coin, per-5-minute flow buckets.

36M prints across 177 coins is 634MB and cannot be re-parsed once per study. This
collapses it once into buckets that every downstream flow question can be answered from,
at ~1M rows instead of 36M.

Bucketed at 5 minutes because the signal bar is 15m aligned to :00/:15/:30/:45, so three
buckets tile a bar exactly, and the 30/60-minute vpin windows are whole multiples. Finer
than that buys nothing any of these features can use.

Per (coin, bucket):
  buy_ntl, sell_ntl   signed notional, side "B" is the buy aggressor (matches
                      live_bot_ats.load_tape_flow)
  n                   print count
  ntl2                sum of squared print notional. With ntl this gives a Herfindahl,
                      sum(x^2)/sum(x)^2, which measures whether the volume came from a
                      few large prints or many small ones -- the thing ats_ratio proxies
                      from candles as volume/count, measured directly here
  big_ntl             notional in prints over $10k (3.1% of prints, and the size
                      distribution is fat enough that this is a different question)

  python3 analysis/tape_agg.py /opt/hyperdata/tape out.csv.gz
"""
import glob, gzip, os, sys

TAPE = sys.argv[1] if len(sys.argv) > 1 else "/opt/hyperdata/tape"
OUT = sys.argv[2] if len(sys.argv) > 2 else "tape_buckets.csv.gz"
BUCKET_MS = 300000
BIG_USD = 10000.0

files = sorted(glob.glob(os.path.join(TAPE, "tape_*.csv.gz")) +
               glob.glob(os.path.join(TAPE, "tape_*.csv")))
print(f"{len(files)} tape files")
out = gzip.open(OUT, "wt", newline="")
out.write("coin,bucket_ms,buy_ntl,sell_ntl,n,ntl2,big_ntl\n")
grand = 0
for path in files:
    op = gzip.open if path.endswith(".gz") else open
    agg = {}
    n = 0
    with op(path, "rt", errors="replace") as f:
        f.readline()
        for line in f:
            p = line.split(",")
            if len(p) < 5:
                continue
            try:
                t = int(p[0]); px = float(p[3]); sz = float(p[4])
            except ValueError:
                continue
            ntl = px * sz
            k = (p[1], t - t % BUCKET_MS)
            b = agg.get(k)
            if b is None:
                b = agg[k] = [0.0, 0.0, 0, 0.0, 0.0]
            if p[2] == "B":
                b[0] += ntl
            else:
                b[1] += ntl
            b[2] += 1
            b[3] += ntl * ntl
            if ntl >= BIG_USD:
                b[4] += ntl
            n += 1
    for (c, bk), v in sorted(agg.items(), key=lambda x: (x[0][1], x[0][0])):
        out.write(f"{c},{bk},{v[0]:.2f},{v[1]:.2f},{v[2]},{v[3]:.2f},{v[4]:.2f}\n")
    grand += n
    print(f"  {os.path.basename(path):<28} {n:>9,} prints -> {len(agg):>7,} buckets")
out.close()
print(f"\n{grand:,} prints -> {OUT} ({os.path.getsize(OUT)/1e6:.0f} MB)")
