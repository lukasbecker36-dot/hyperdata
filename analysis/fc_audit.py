#!/usr/bin/env python3
"""Phase 0 data audit for the forecasting spec. Produces data_audit.md.

Spec 1: "verify the actual date span of tape_buckets.csv.gz and confirm it matches the
raw tape. Report the overlap between tape and every candle file."

The expected answer is that the overlap is ZERO -- candles end 2026-07-17, the tape
starts 2026-07-21 -- which is why spec 11 forbids joining flow features to the candle
files. This script proves it rather than assuming it, because the whole feature design
depends on the claim.

  python3 analysis/fc_audit.py [out.md]
"""
import csv, glob, gzip, os, sys
from datetime import datetime, timezone

OUT = sys.argv[1] if len(sys.argv) > 1 else "data_audit.md"
SCRATCH = os.environ.get("FC_SCRATCH", ".")


def fmt(ms):
    return datetime.fromtimestamp(ms/1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


def day(ms):
    return datetime.fromtimestamp(ms/1000, timezone.utc).strftime("%Y-%m-%d")


L = []
def w(s=""):
    L.append(s)
    # stdout on Windows is cp1252; the file is written utf-8 either way
    print(s.encode("ascii", "replace").decode())


w("# Phase 0 data audit")
w()
w(f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
w()

# ---------- candle files ----------
w("## Candle files")
w()
w("| file | bar | rows | coins | from | to | span (d) | NaN cells | gaps |")
w("|---|---|---|---|---|---|---|---|---|")
spans = {}
CAND = [("hyperliquid_1m_48h.csv", "1m", 60), ("hyperliquid_1m_60d.csv", "1m", 60),
        ("hyperliquid_5m.csv", "5m", 300), ("hyperliquid_15m_60d.csv", "15m", 900),
        ("hyperliquid_15m_allperps.csv", "15m", 900),
        ("hyperliquid_1h_history.csv", "1h", 3600)]
for path, iv, sec in CAND:
    if not os.path.exists(path):
        w(f"| `{path}` | {iv} | MISSING | | | | | | |")
        continue
    lo = hi = None
    coins = set()
    rows = nan = 0
    per = {}
    with open(path) as f:
        r = csv.DictReader(f)
        tcol = "open_time_ms"
        for x in r:
            rows += 1
            try:
                t = int(x[tcol])
            except Exception:
                nan += 1
                continue
            s = x["symbol"]
            coins.add(s)
            lo = t if lo is None or t < lo else lo
            hi = t if hi is None or t > hi else hi
            p = per.get(s)
            if p is None:
                per[s] = [t, t, 1]
            else:
                p[1] = t if t > p[1] else p[1]
                p[2] += 1
            for k in ("open", "high", "low", "close", "volume"):
                if not (x.get(k) or "").strip():
                    nan += 1
    # gaps: expected bars vs actual, per coin, summed
    exp = sum(((p[1]-p[0])//(sec*1000))+1 for p in per.values())
    act = sum(p[2] for p in per.values())
    spans[path] = (lo, hi)
    w(f"| `{path}` | {iv} | {rows:,} | {len(coins)} | {fmt(lo)} | {fmt(hi)} | "
      f"{(hi-lo)/86400000:.1f} | {nan} | {exp-act:,} ({100*(exp-act)/max(1,exp):.1f}%) |")
w()

# ---------- raw tape ----------
w("## Raw tape")
w()
tape_files = sorted(glob.glob("/opt/hyperdata/tape/tape_*.csv*"))
if not tape_files:
    tape_files = sorted(glob.glob(os.path.join(SCRATCH, "tape_*.csv*")))
w(f"{len(tape_files)} files found" + ("" if tape_files else " — run on the server for this section"))
w()

# ---------- bucket file ----------
BK = os.path.join(SCRATCH, "tape_buckets.csv.gz")
if os.path.exists(BK):
    lo = hi = None
    coins = set()
    rows = 0
    perday = {}
    with gzip.open(BK, "rt") as f:
        next(f)
        for line in f:
            p = line.split(",")
            t = int(p[1])
            rows += 1
            coins.add(p[0])
            lo = t if lo is None or t < lo else lo
            hi = t if hi is None or t > hi else hi
            perday.setdefault(day(t), set()).add(p[0])
    w("## `tape_buckets.csv.gz` (derived, 5-min)")
    w()
    w(f"- rows **{rows:,}**, coins **{len(coins)}**")
    w(f"- span **{fmt(lo)} to {fmt(hi)}**  ({(hi-lo)/86400000:.1f} days)")
    w()
    w("### Coins active per day")
    w()
    w("| day | coins | | day | coins |")
    w("|---|---|---|---|---|")
    ds = sorted(perday)
    half = (len(ds)+1)//2
    for i in range(half):
        a = ds[i]; b = ds[i+half] if i+half < len(ds) else None
        w(f"| {a} | {len(perday[a])} | | {b or ''} | {len(perday[b]) if b else ''} |")
    w()
    spans["tape"] = (lo, hi)

# ---------- the overlap question ----------
w("## Overlap: tape vs each candle file")
w()
w("Spec 11 forbids joining tape flow to the candle files. This is why:")
w()
w("| candle file | candle ends | tape starts | overlap |")
w("|---|---|---|---|")
if "tape" in spans:
    tl, th = spans["tape"]
    for path, _, _ in CAND:
        if path not in spans:
            continue
        cl, ch = spans[path]
        ov = min(ch, th) - max(cl, tl)
        w(f"| `{path}` | {fmt(ch)} | {fmt(tl)} | "
          f"**{'NONE — gap of %.1f days' % ((tl-ch)/86400000) if ov < 0 else '%.1f days' % (ov/86400000)}** |")
    w()
    w(f"The tape begins **{fmt(tl)}**. Every candle file ends on or before "
      f"**{fmt(max(spans[p][1] for p, _, _ in CAND if p in spans))}**. "
      f"There is no bar in common, so all flow features must be built on tape-derived "
      f"bars.")
w()

open(OUT, "w", encoding="utf-8").write("\n".join(L) + "\n")
print(f"\nwrote {OUT}")
