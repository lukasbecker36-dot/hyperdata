#!/usr/bin/env python3
"""Shadow-fill audit v2 — same question as shadow_fill.py, but with the ORDER-PLACEMENT
time fixed, and with tape-coverage confounds separated out.

Two problems with v1 that this fixes:

1. **Wrong t0.** v1 opens the entry fill window at `entry_time` from the trade CSV, which the
   bot writes as `feat["close_ms"]` — the *bar close*, not the moment it placed the order. The
   bot only wakes ~15s after the bar, then serially polls ~177 candle endpoints plus a book
   call per fill, so real placement lands anywhere from ~20s to ~200s after the bar close. At
   W=60 the v1 window can therefore close *before the order existed*; at W=300 it credits up to
   ~200s of tape during which there was no resting order. We recover the true placement time
   from the bot log's `OPEN` lines and use that instead.

2. **"none" conflates 'did not fill' with 'coin absent from tape'.** The tape logger subscribes
   to the universe snapshot at connect time, so a coin can be missing for a whole session. A
   trade in a coin with zero tape rows is *unmeasurable*, not unfilled. We report those
   separately instead of scoring them as misses.

Fill rule is unchanged (that part of v1 is right): a resting order fills only when an opposite
aggressor prints through it —
  resting SELL @ P (short entry / long exit)  -> needs a B trade at px >= P
  resting BUY  @ P (long entry  / short exit) -> needs an A trade at px <= P

Run on the server (the tape stays local):
  python3 shadow_fill2.py [trades_glob]      (default live/*.csv)
"""
import csv, gzip, glob, os, bisect, re, sys
from collections import defaultdict
from datetime import datetime, timezone

TRADES_GLOB = sys.argv[1] if len(sys.argv) > 1 else "live/*.csv"
TAPE_GLOB   = "tape/tape_*.csv*"
LOG_GLOB    = "{arm}/bot_*.log"
WINDOWS_S   = [60, 300, 900]
WMAX_MS     = max(WINDOWS_S) * 1000
MAX_PLACE_DELAY_S = 1800        # sanity cap when joining an OPEN line to a trade row

OPEN_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+OPEN\s+(\S+)\s+(SHORT|LONG)\s+@\s+(\S+)")


def pms(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ---- load trades from every arm ----
trades = []
for path in sorted(glob.glob(TRADES_GLOB)):
    if "shadow_fill" in os.path.basename(path):
        continue
    arm = os.path.basename(path).replace(".csv", "")
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                trades.append(dict(
                    arm=arm, sym=r["symbol"], side=r["side"],
                    bar_ms=pms(r["entry_time"]), close_ms=pms(r["close_time"]),
                    entry_px=float(r["entry_px"]),
                    e_bid=float(r["entry_bid"]), e_ask=float(r["entry_ask"]),
                    x_bid=float(r["exit_bid"]), x_ask=float(r["exit_ask"]),
                    pnl=float(r["pnl_usd"]), reason=r["reason"]))
            except Exception:
                pass
if not trades:
    print("no trades found (glob:", TRADES_GLOB, ")"); sys.exit(0)

# ---- recover real order-placement times from each arm's bot log ----
opens = defaultdict(list)          # (arm, sym, side) -> [(ts_ms, px)]
for arm in sorted({t["arm"] for t in trades}):
    for lf in sorted(glob.glob(LOG_GLOB.format(arm=arm))):
        try:
            with open(lf, errors="replace") as f:
                for line in f:
                    m = OPEN_RE.match(line)
                    if not m:
                        continue
                    ts, sym, side, px = m.groups()
                    try: pxf = float(px)
                    except ValueError: continue
                    opens[(arm, sym, side)].append((pms(ts), pxf))
        except Exception as e:
            print(f"WARN reading {lf}: {e}")
for k in opens:
    opens[k].sort()

# join: for each trade (chronological), take the nearest unconsumed OPEN at/after the bar close.
# Prefer a price match (log prints %.6g vs CSV %.8g) to disambiguate re-entries in the same coin.
used = defaultdict(set)
n_placed = 0
for t in sorted(trades, key=lambda x: (x["arm"], x["sym"], x["bar_ms"])):
    cand = opens.get((t["arm"], t["sym"], t["side"]), [])
    best = None
    for i, (ts, pxf) in enumerate(cand):
        if i in used[(t["arm"], t["sym"], t["side"])]:
            continue
        d = ts - t["bar_ms"]
        if d < 0 or d > MAX_PLACE_DELAY_S * 1000:
            continue
        px_ok = abs(pxf - t["entry_px"]) <= 1e-4 * max(abs(t["entry_px"]), 1e-12)
        # rank: price match first, then smallest delay
        key = (0 if px_ok else 1, d)
        if best is None or key < best[0]:
            best = (key, i, ts)
    if best is not None:
        used[(t["arm"], t["sym"], t["side"])].add(best[1])
        t["place_ms"] = best[2]
        t["delay_s"] = (best[2] - t["bar_ms"]) / 1000.0
        n_placed += 1
    else:
        t["place_ms"] = None
        t["delay_s"] = None

delays = sorted(t["delay_s"] for t in trades if t["delay_s"] is not None)


def pct(xs, p):
    if not xs: return float("nan")
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))]


# ---- stream tape; keep rows inside any fill window, and count per-symbol presence ----
raw_wins = defaultdict(list)
for t in trades:
    t0 = t["place_ms"] if t["place_ms"] is not None else t["bar_ms"]
    raw_wins[t["sym"]].append((t0, t0 + WMAX_MS))
    raw_wins[t["sym"]].append((t["close_ms"], t["close_ms"] + WMAX_MS))
win_s, win_e = {}, {}
for sym, ws in raw_wins.items():
    ws.sort(); merged = [list(ws[0])]
    for a, b in ws[1:]:
        if a <= merged[-1][1]: merged[-1][1] = max(merged[-1][1], b)
        else: merged.append([a, b])
    win_s[sym] = [m[0] for m in merged]; win_e[sym] = [m[1] for m in merged]

kept = defaultdict(list)
seen_any = defaultdict(int)          # tape rows per symbol ANYWHERE (coverage confound check)
tape_min = tape_max = None
for tf in sorted(glob.glob(TAPE_GLOB)):
    op = gzip.open if tf.endswith(".gz") else open
    try:
        with op(tf, "rt") as f:
            for r in csv.reader(f):
                if not r or r[0] == "time_ms": continue
                try:
                    t = int(r[0]); sym = r[1]; side = r[2]; px = float(r[3])
                except Exception:
                    continue
                tape_min = t if tape_min is None else min(tape_min, t)
                tape_max = t if tape_max is None else max(tape_max, t)
                ss = win_s.get(sym)
                if ss is None: continue
                seen_any[sym] += 1
                i = bisect.bisect_right(ss, t) - 1
                if i >= 0 and t <= win_e[sym][i]:
                    kept[sym].append((t, side, px))
    except Exception as e:
        print(f"WARN reading {tf}: {e}")
for sym in kept:
    kept[sym].sort()
if tape_min is None:
    print("no tape found (glob:", TAPE_GLOB, ")"); sys.exit(0)


def fills(sym, level, is_sell, t0, W):
    """(touched, through) in [t0, t0+W]. resting SELL needs B>=level; BUY needs A<=level."""
    rows = kept.get(sym)
    if not rows: return (False, False)
    lo = bisect.bisect_left(rows, (t0,)); tend = t0 + W * 1000
    touched = through = False
    for j in range(lo, len(rows)):
        tt, side, px = rows[j]
        if tt > tend: break
        if is_sell and side == "B":
            if px >= level: touched = True
            if px > level: through = True
        elif (not is_sell) and side == "A":
            if px <= level: touched = True
            if px < level: through = True
        if through: break
    return (touched, through)


def covered(t):
    t0 = t["place_ms"] if t["place_ms"] is not None else t["bar_ms"]
    return tape_min <= t0 and t["close_ms"] + WMAX_MS <= tape_max


# ---- audit ----
rep_rows = []
by_arm = defaultdict(lambda: {"n": 0, "no_tape": 0, "no_place": 0})
n_cov = n_cov_measurable = 0
for t in trades:
    if not covered(t):
        continue
    n_cov += 1
    A = by_arm[t["arm"]]
    if seen_any.get(t["sym"], 0) == 0:      # coin never appears in the tape -> unmeasurable
        A["no_tape"] += 1
        continue
    n_cov_measurable += 1
    A["n"] += 1
    if t["place_ms"] is None:
        A["no_place"] += 1
    t0 = t["place_ms"] if t["place_ms"] is not None else t["bar_ms"]

    short = t["side"] == "SHORT"
    e_lvl, e_sell = (t["e_ask"], True) if short else (t["e_bid"], False)
    x_lvl, x_sell = (t["x_bid"], False) if short else (t["x_ask"], True)
    row = {"arm": t["arm"], "sym": t["sym"], "side": t["side"], "reason": t["reason"],
           "pnl": t["pnl"], "delay_s": "" if t["delay_s"] is None else f"{t['delay_s']:.0f}"}
    for W in WINDOWS_S:
        et, eth = fills(t["sym"], e_lvl, e_sell, t0, W)
        xt, xth = fills(t["sym"], x_lvl, x_sell, t["close_ms"], W)
        row[f"entry_fill_{W}"] = "through" if eth else ("touch" if et else "none")
        row[f"exit_fill_{W}"] = "through" if xth else ("touch" if xt else "none")
        d = A.setdefault(W, defaultdict(float))
        d["ef_at"] += et; d["ef_th"] += eth; d["xf_at"] += xt; d["xf_th"] += xth
        d["pnl_all"] += t["pnl"]
        d["pnl_efat"] += t["pnl"] if et else 0.0
        d["pnl_efth"] += t["pnl"] if eth else 0.0
        # round-trip realism: entry AND exit both have to print through
        d["pnl_rt"] += t["pnl"] if (eth and xth) else 0.0
        d["rt_n"] += 1 if (eth and xth) else 0
        d["miss_pnl"] += 0.0 if et else t["pnl"]
        d["miss_n"] += 0 if et else 1
    rep_rows.append(row)

print(f"trades: {len(trades)}  |  placement time recovered: {n_placed} "
      f"({n_placed/len(trades)*100:.0f}%)  |  tape-covered: {n_cov}  |  measurable: {n_cov_measurable}")
print(f"tape span: {fmt(tape_min)} -> {fmt(tape_max)} UTC")
if delays:
    print(f"bar-close -> order-placement delay (s):  p50={pct(delays,50):.0f}  "
          f"p90={pct(delays,90):.0f}  p99={pct(delays,99):.0f}  max={delays[-1]:.0f}")
    print("  ^ v1 used bar close as t0, so it mis-credits this much tape to the resting order.")
no_tape_syms = sorted({t["sym"] for t in trades
                       if covered(t) and seen_any.get(t["sym"], 0) == 0})
if no_tape_syms:
    print(f"\ncoins absent from tape ({len(no_tape_syms)}), excluded as unmeasurable: "
          f"{', '.join(no_tape_syms)}")
print()

for W in WINDOWS_S:
    print(f"=== resting window {W}s (from real placement time) ===")
    print(f"  {'arm':20s} {'n':>4} {'entryFill t/th':>15} {'exitFill t/th':>15} "
          f"{'assumed$':>9} {'entryFilt$':>11} {'roundtrip$':>11} {'missed(n,$)':>14}")
    for arm, A in sorted(by_arm.items()):
        if A["n"] == 0: continue
        d = A[W]; n = A["n"]
        print(f"  {arm:20s} {n:>4} {d['ef_at']/n*100:>7.0f}%/{d['ef_th']/n*100:>3.0f}% "
              f"{d['xf_at']/n*100:>10.0f}%/{d['xf_th']/n*100:>3.0f}% "
              f"{d['pnl_all']:>+9.2f} {d['pnl_efth']:>+11.2f} {d['pnl_rt']:>+11.2f} "
              f"{int(d['miss_n']):>4d},{d['miss_pnl']:>+8.2f}")
    print()

print("COLUMNS")
print("  assumed$    = P&L the bot booked (assumes instant maker fill at the touch)")
print("  entryFilt$  = P&L keeping only trades whose ENTRY printed through")
print("  roundtrip$  = P&L keeping only trades where entry AND exit both printed through")
print("  missed$     = P&L of trades whose entry would NOT have filled. Strongly POSITIVE")
print("                means the fade is adversely selected: you fill the continuations")
print("                (losers) and miss the reversions (winners).")

if rep_rows:
    out = "live/shadow_fill_report_v2.csv"
    os.makedirs("live", exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rep_rows[0].keys()))
        w.writeheader(); w.writerows(rep_rows)
    print(f"\nwrote {out} ({len(rep_rows)} rows)")
