#!/usr/bin/env python3
"""Shadow-fill audit: did the maker orders the paper bot ASSUMED filled actually fill?

The bot assumes a resting maker order fills at the touch. The real tape settles it, because it
records price + aggressor side (B=buy lifts an ask, A=sell hits a bid). A resting order fills only
when the opposite aggressor prints through it:
  resting SELL @ P (short entry / long exit)  -> needs a B trade at px >= P
  resting BUY  @ P (long entry  / short exit) -> needs an A trade at px <= P
We report, per arm: fill rate (optimistic 'touched' vs conservative 'traded through'), and the
adverse-selection cost — whether the trades that would NOT have filled are the winners (the fade's
structural risk: fill the continuations, miss the reversions).

Runs on the server (big tape stays local); writes a small live/shadow_fill_report.csv + summary.
  python3 shadow_fill.py [trades_glob]   (default live/*.csv from collect_trades.sh)
"""
import csv, gzip, glob, os, bisect, sys
from collections import defaultdict
from datetime import datetime, timezone

TRADES_GLOB = sys.argv[1] if len(sys.argv) > 1 else "live/*.csv"
TAPE_GLOB   = "tape/tape_*.csv*"
WINDOWS_S   = [60, 300, 900]          # resting windows to test (seconds)
WMAX_MS     = max(WINDOWS_S) * 1000

def pms(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)

# ---- load trades from every arm ----
trades = []
for path in sorted(glob.glob(TRADES_GLOB)):
    if path.endswith("shadow_fill_report.csv"): continue
    arm = os.path.basename(path).replace(".csv", "").replace("trades_", "")
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                trades.append(dict(arm=arm, sym=r["symbol"], side=r["side"],
                    entry_ms=pms(r["entry_time"]), close_ms=pms(r["close_time"]),
                    e_bid=float(r["entry_bid"]), e_ask=float(r["entry_ask"]),
                    x_bid=float(r["exit_bid"]), x_ask=float(r["exit_ask"]),
                    pnl=float(r["pnl_usd"]), reason=r["reason"]))
            except Exception:
                pass
if not trades:
    print("no trades found (glob:", TRADES_GLOB, ")"); sys.exit(0)

# ---- fill windows per coin (entry + exit), merged, to keep only relevant tape rows ----
raw_wins = defaultdict(list)
for t in trades:
    raw_wins[t["sym"]].append((t["entry_ms"], t["entry_ms"] + WMAX_MS))
    raw_wins[t["sym"]].append((t["close_ms"], t["close_ms"] + WMAX_MS))
win_s, win_e = {}, {}
for sym, ws in raw_wins.items():
    ws.sort(); merged = [list(ws[0])]
    for a, b in ws[1:]:
        if a <= merged[-1][1]: merged[-1][1] = max(merged[-1][1], b)
        else: merged.append([a, b])
    win_s[sym] = [m[0] for m in merged]; win_e[sym] = [m[1] for m in merged]

# ---- stream tape, keep only rows inside a fill window; track coverage span ----
kept = defaultdict(list); tape_min = tape_max = None
for tf in sorted(glob.glob(TAPE_GLOB)):
    op = gzip.open if tf.endswith(".gz") else open
    try:
        with op(tf, "rt") as f:
            for r in csv.reader(f):
                if not r or r[0] == "time_ms": continue
                try: t = int(r[0]); sym = r[1]; side = r[2]; px = float(r[3])
                except Exception: continue
                tape_min = t if tape_min is None else min(tape_min, t)
                tape_max = t if tape_max is None else max(tape_max, t)
                ss = win_s.get(sym)
                if not ss: continue
                i = bisect.bisect_right(ss, t) - 1
                if i >= 0 and t <= win_e[sym][i]:
                    kept[sym].append((t, side, px))
    except Exception as e:
        print(f"WARN reading {tf}: {e}")
for sym in kept: kept[sym].sort()
if tape_min is None:
    print("no tape found (glob:", TAPE_GLOB, ") — nothing to audit yet."); sys.exit(0)

def fills(sym, level, is_sell, t0, W):
    """Return (touched, through) within [t0, t0+W]. resting SELL needs B>=level; BUY needs A<=level."""
    rows = kept.get(sym)
    if not rows: return (False, False)
    lo = bisect.bisect_left(rows, (t0,)); tend = t0 + W * 1000
    touched = through = False
    for j in range(lo, len(rows)):
        tt, side, px = rows[j]
        if tt > tend: break
        if is_sell and side == "B":
            if px >= level: touched = True
            if px >  level: through = True
        elif (not is_sell) and side == "A":
            if px <= level: touched = True
            if px <  level: through = True
        if through: break
    return (touched, through)

# ---- audit each covered trade ----
def covered(t):
    return tape_min <= t["entry_ms"] and t["close_ms"] + WMAX_MS <= tape_max

rep_rows = []
by_arm = defaultdict(lambda: {"n": 0})
for t in trades:
    if not covered(t): continue
    short = t["side"] == "SHORT"
    e_lvl, e_sell = (t["e_ask"], True) if short else (t["e_bid"], False)   # short entry sells @ ask
    x_lvl, x_sell = (t["x_bid"], False) if short else (t["x_ask"], True)   # short exit buys @ bid
    row = {"arm": t["arm"], "sym": t["sym"], "side": t["side"], "reason": t["reason"], "pnl": t["pnl"]}
    A = by_arm[t["arm"]]; A["n"] += 1
    for W in WINDOWS_S:
        et, eth = fills(t["sym"], e_lvl, e_sell, t["entry_ms"], W)
        xt, xth = fills(t["sym"], x_lvl, x_sell, t["close_ms"], W)
        row[f"entry_fill_{W}"] = "through" if eth else ("touch" if et else "none")
        row[f"exit_fill_{W}"]  = "through" if xth else ("touch" if xt else "none")
        d = A.setdefault(W, defaultdict(float))
        d["ef_at"] += et; d["ef_th"] += eth; d["xf_at"] += xt; d["xf_th"] += xth
        d["pnl_all"] += t["pnl"]
        d["pnl_efat"] += t["pnl"] if et else 0.0     # P&L if we keep only entry-filled (optimistic)
        d["pnl_efth"] += t["pnl"] if eth else 0.0    # conservative
        d["miss_pnl"] += 0.0 if et else t["pnl"]     # P&L of trades we'd have MISSED (entry didn't fill)
        d["miss_n"] += 0 if et else 1
    rep_rows.append(row)

ncov = sum(1 for t in trades if covered(t))
print(f"trades: {len(trades)}  |  with tape coverage: {ncov}  |  "
      f"tape span: {datetime.utcfromtimestamp((tape_min or 0)/1000):%Y-%m-%d %H:%M} -> "
      f"{datetime.utcfromtimestamp((tape_max or 0)/1000):%Y-%m-%d %H:%M} UTC\n")
if ncov == 0:
    print("No trades fall fully inside the tape window yet — re-run once the tape overlaps the trades.")
else:
    for W in WINDOWS_S:
        print(f"=== resting window {W}s ===")
        print(f"  {'arm':16s} {'n':>4} {'entryFill(touch/thru)':>22} {'exitFill':>16} "
              f"{'assumed$':>9} {'filled$(t/th)':>16} {'missed(n,$)':>14}")
        for arm, A in sorted(by_arm.items()):
            if A["n"] == 0: continue
            d = A[W]; n = A["n"]
            print(f"  {arm:16s} {n:>4} {d['ef_at']/n*100:>9.0f}%/{d['ef_th']/n*100:>3.0f}%      "
                  f"{d['xf_at']/n*100:>6.0f}%/{d['xf_th']/n*100:>3.0f}%   "
                  f"{d['pnl_all']:>+9.2f} {d['pnl_efat']:>+7.2f}/{d['pnl_efth']:>+7.2f}   "
                  f"{int(d['miss_n']):>3d},{d['miss_pnl']:>+8.2f}")
        print()
    print("KEY: 'missed$' = P&L of trades whose entry would NOT have filled. If that's strongly")
    print("     POSITIVE, the fade is adversely selected (missing winners) and real P&L << assumed.")

# small enriched output to ship back
if rep_rows:
    out = "live/shadow_fill_report.csv"
    os.makedirs("live", exist_ok=True)
    cols = list(rep_rows[0].keys())
    with open(out, "w", newline="") as f:
        wtr = csv.DictWriter(f, fieldnames=cols); wtr.writeheader(); wtr.writerows(rep_rows)
    print(f"\nwrote {out} ({len(rep_rows)} covered trades) — push it back for the full breakdown.")
