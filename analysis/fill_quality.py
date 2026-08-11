#!/usr/bin/env python3
"""Why do 13% of resting entries never fill? Queue position, or the touch never returning?

Those two have opposite fixes and the bot cannot currently tell them apart:

  QUEUE LOSS   prints DID trade at or through our resting price while we sat there, and we
               were too far back in the queue to be filled. Money lost with no offsetting
               benefit -> peg more aggressively, or cross.
  NO TOUCH     nothing ever traded at our price. Nothing was lost; the market simply left.
               -> patience is correct and crossing would only pay the spread for nothing.

This is a mechanical question, not a statistical one. Each window is individually
diagnosable from the tape: for a resting BUY at P, any SELL-aggressor print at px <= P
should have filled a front-of-queue order; for a resting SELL at P, any BUY-aggressor print
at px >= P. Entries are placed once at the near touch and rest up to 300s without
re-pegging (the `repegs` counter is exit-side), so the resting price is fixed for the whole
window and the test is unambiguous.

Run on the server, where the tape lives. Emits one row per resting window:
  fill_ntl    notional that traded at/through our price on the filling side
  fill_n      number of such prints
  first_ms    ms from placement to the first such print
  any_ntl     total notional in the coin over the window, for scale

  python3 analysis/fill_quality.py /opt/hyperdata/tape windows.csv out.csv
"""
import bisect, glob, gzip, os, sys

TAPE = sys.argv[1] if len(sys.argv) > 1 else "/opt/hyperdata/tape"
WIN = sys.argv[2] if len(sys.argv) > 2 else "windows.csv"
OUT = sys.argv[3] if len(sys.argv) > 3 else "fill_quality.csv"

# windows.csv: id,coin,t0,t1,px,is_buy,filled
rows = []
with open(WIN) as f:
    hdr = f.readline().rstrip("\n").split(",")
    ix = {c: i for i, c in enumerate(hdr)}
    for line in f:
        p = line.rstrip("\n").split(",")
        rows.append((p[ix["id"]], p[ix["coin"]], int(p[ix["t0"]]), int(p[ix["t1"]]),
                     float(p[ix["px"]]), int(p[ix["is_buy"]]), int(p[ix["filled"]])))
print(f"{len(rows)} resting windows, {len({r[1] for r in rows})} coins")

by_coin = {}
for r in rows:
    by_coin.setdefault(r[1], []).append(r)
for c in by_coin:
    by_coin[c].sort(key=lambda r: r[2])
starts = {c: [r[2] for r in v] for c, v in by_coin.items()}

acc = {r[0]: [0.0, 0, None, 0.0] for r in rows}     # fill_ntl, fill_n, first_ms, any_ntl
files = sorted(glob.glob(os.path.join(TAPE, "tape_*.csv.gz")) +
               glob.glob(os.path.join(TAPE, "tape_*.csv")))
scanned = 0
for path in files:
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", errors="replace") as f:
        f.readline()
        for line in f:
            p = line.split(",")
            if len(p) < 5:
                continue
            v = by_coin.get(p[1])
            if v is None:
                continue
            try:
                t = int(p[0]); px = float(p[3]); sz = float(p[4])
            except ValueError:
                continue
            st = starts[p[1]]
            # windows on one coin can overlap; walk back a few candidates
            j = bisect.bisect_right(st, t) - 1
            for k in range(j, max(-1, j - 4), -1):
                if k < 0:
                    break
                _id, _c, t0, t1, wpx, is_buy, _f = v[k]
                if not (t0 <= t <= t1):
                    continue
                a = acc[_id]
                ntl = px * sz
                a[3] += ntl
                # would this print have filled a front-of-queue order at wpx?
                hit = (p[2] == "A" and px <= wpx) if is_buy else (p[2] == "B" and px >= wpx)
                if hit:
                    a[0] += ntl
                    a[1] += 1
                    if a[2] is None:
                        a[2] = t - t0
            scanned += 1
    print(f"  {os.path.basename(path):<28} scanned {scanned:,}")

with open(OUT, "w") as f:
    f.write("id,fill_ntl,fill_n,first_ms,any_ntl\n")
    for r in rows:
        a = acc[r[0]]
        f.write(f"{r[0]},{a[0]:.2f},{a[1]},"
                f"{'' if a[2] is None else a[2]},{a[3]:.2f}\n")
print(f"\nwrote {OUT}")
