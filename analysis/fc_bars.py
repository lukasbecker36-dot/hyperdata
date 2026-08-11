#!/usr/bin/env python3
"""Rebuild point-in-time bars from the raw tape. Phase 0 of the forecasting spec.

The candle files END 2026-07-17 and the tape STARTS 2026-07-21, so they do not overlap
by a single bar. Every flow feature therefore has to be computed on bars rebuilt from
prints -- joining tape flow to the candle files would be fabricating data (spec 11).

Bars are keyed by OPEN time. A bar [t, t+D) is complete only at t+D, and a print at
exactly t belongs to the bar starting at t. Labels are open-to-open, so the bar's OPEN
price is the tradeable entry and is recorded explicitly rather than inferred from the
previous close -- on a 30s bar in an illiquid perp those differ materially.

Emitted per (coin, bar), stdlib only because the server has no numpy:

  o,h,l,c        from prints, in arrival order
  n              print count
  buy_ntl        notional with side B (buy aggressor)
  sell_ntl       notional with side A (sell aggressor)
  ntl2           SUM(notional^2) -> eff_print_size = ntl2/ntl, herfindahl = ntl2/ntl^2
  big_buy_ntl    notional in prints >= $10k, SIGNED. The bucket file carries only an
  big_sell_ntl   unsigned big_ntl, which throws away the informative half (spec 3.1);
                 this is the recommended schema change.
  dpx_dntl       SUM(dpx * signed_ntl) and SUM(signed_ntl^2) within the bar, so Kyle's
  sntl2          lambda can be accumulated across bars without re-reading 36M prints

Usage:
  python3 analysis/fc_bars.py /opt/hyperdata/tape 60 bars_1m.csv.gz
"""
import glob, gzip, os, sys

TAPE = sys.argv[1] if len(sys.argv) > 1 else "/opt/hyperdata/tape"
SEC = int(sys.argv[2]) if len(sys.argv) > 2 else 60
OUT = sys.argv[3] if len(sys.argv) > 3 else f"bars_{SEC}s.csv.gz"
BAR_MS = SEC * 1000
BIG_USD = 10000.0

COLS = ("coin,bar_ms,o,h,l,c,n,buy_ntl,sell_ntl,ntl2,"
        "big_buy_ntl,big_sell_ntl,dpx_sntl,sntl2\n")

files = sorted(glob.glob(os.path.join(TAPE, "tape_*.csv.gz")) +
               glob.glob(os.path.join(TAPE, "tape_*.csv")))
print(f"{len(files)} tape files -> {SEC}s bars")
out = gzip.open(OUT, "wt", newline="")
out.write(COLS)
grand = nbars = 0
for path in files:
    op = gzip.open if path.endswith(".gz") else open
    agg = {}
    last_px = {}          # coin -> previous print px, for the dpx accumulation
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
            coin = p[1]
            ntl = px * sz
            buy = (p[2] == "B")
            sntl = ntl if buy else -ntl
            lp = last_px.get(coin)
            dpx = 0.0 if lp is None else (px - lp) / lp
            last_px[coin] = px
            k = (coin, t - t % BAR_MS)
            b = agg.get(k)
            if b is None:
                # o h l c n buy sell ntl2 bigbuy bigsell dpx_sntl sntl2
                b = agg[k] = [px, px, px, px, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
            if px > b[1]:
                b[1] = px
            if px < b[2]:
                b[2] = px
            b[3] = px
            b[4] += 1
            if buy:
                b[5] += ntl
                if ntl >= BIG_USD:
                    b[8] += ntl
            else:
                b[6] += ntl
                if ntl >= BIG_USD:
                    b[9] += ntl
            b[7] += ntl * ntl
            b[10] += dpx * sntl
            b[11] += sntl * sntl
            n += 1
    for (c, bk), v in sorted(agg.items(), key=lambda x: (x[0][1], x[0][0])):
        out.write(f"{c},{bk},{v[0]:.10g},{v[1]:.10g},{v[2]:.10g},{v[3]:.10g},{v[4]},"
                  f"{v[5]:.2f},{v[6]:.2f},{v[7]:.2f},{v[8]:.2f},{v[9]:.2f},"
                  f"{v[10]:.6g},{v[11]:.2f}\n")
    grand += n
    nbars += len(agg)
    print(f"  {os.path.basename(path):<28} {n:>9,} prints -> {len(agg):>8,} bars")
out.close()
print(f"\n{grand:,} prints -> {nbars:,} bars -> {OUT} "
      f"({os.path.getsize(OUT)/1e6:.0f} MB)")
