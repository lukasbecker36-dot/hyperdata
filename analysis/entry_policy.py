#!/usr/bin/env python3
"""Entry-execution policy sweep: ABANDON vs CROSS, as a function of the maker timeout.

`analysis/taker_entry.py` answers "is a taker entry worth it at the current 300s timeout?"
(no). This script asks the better question — the timeout length and the fallback are ONE
decision, so sweep them together:

  ABANDON(W): rest as a maker for W seconds; if unfilled, skip the trade.   <- live bot today
  CROSS(W)  : rest as a maker for W seconds; if unfilled, cross the spread.

Both policies are scored over an IDENTICAL trade set (trades measurable at every W), so the
columns are directly comparable. That matters: comparing ABANDON(300) to CROSS(60) across
different measurable subsets was what made the naive version of this look better than it is.

Fill rule and taker pricing are the audited ones (see shadow_fill2.py / taker_entry.py):
a resting order fills only when an opposite aggressor prints through it; a taker sell hits
the bid ('A' prints) and a taker buy lifts the ask ('B' prints).

  python3 analysis/entry_policy.py [trades_glob]
"""
import csv, gzip, glob, os, bisect, re, sys
from collections import defaultdict
from datetime import datetime, timezone

TRADES_GLOB = sys.argv[1] if len(sys.argv) > 1 else "live/*.csv"
TAPE_GLOB   = "tape/tape_*.csv*"
LOG_GLOB    = "{arm}/bot_*.log"
WINDOWS     = ([int(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2
               else [30, 60, 120, 300, 600, 900])
WMAX_MS     = 900 * 1000
STALE_MS    = 60 * 1000
MAKER_BPS   = 1.5
TAKER_BPS   = 4.5
MAX_PLACE_DELAY_S = 1800
OPEN_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+OPEN\s+(\S+)\s+(SHORT|LONG)\s+@\s+(\S+)")


def pms(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


trades = []
for path in sorted(glob.glob(TRADES_GLOB)):
    b = os.path.basename(path)
    if "shadow_fill" in b or "taker" in b or "policy" in b:
        continue
    arm = b.replace(".csv", "")
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                net = float(r["net_bps"]) / 1e4
                pnl = float(r["pnl_usd"])
                if abs(net) < 1e-12:
                    continue
                trades.append(dict(
                    arm=arm, sym=r["symbol"], side=r["side"],
                    bar_ms=pms(r["entry_time"]), close_ms=pms(r["close_time"]),
                    entry_px=float(r["entry_px"]), exit_px=float(r["exit_px"]),
                    pnl=pnl, notional=pnl / net))
            except Exception:
                pass

opens = defaultdict(list)
for arm in sorted({t["arm"] for t in trades}):
    for lf in sorted(glob.glob(LOG_GLOB.format(arm=arm))):
        try:
            with open(lf, errors="replace") as f:
                for line in f:
                    m = OPEN_RE.match(line)
                    if not m: continue
                    ts, sym, side, px = m.groups()
                    try: opens[(arm, sym, side)].append((pms(ts), float(px)))
                    except ValueError: pass
        except Exception as e:
            print(f"WARN {lf}: {e}")
for k in opens: opens[k].sort()

used = defaultdict(set)
for t in sorted(trades, key=lambda x: (x["arm"], x["sym"], x["bar_ms"])):
    best = None
    for i, (ts, pxf) in enumerate(opens.get((t["arm"], t["sym"], t["side"]), [])):
        if i in used[(t["arm"], t["sym"], t["side"])]: continue
        d = ts - t["bar_ms"]
        if d < 0 or d > MAX_PLACE_DELAY_S * 1000: continue
        ok = abs(pxf - t["entry_px"]) <= 1e-4 * max(abs(t["entry_px"]), 1e-12)
        key = (0 if ok else 1, d)
        if best is None or key < best[0]: best = (key, i, ts)
    if best:
        used[(t["arm"], t["sym"], t["side"])].add(best[1]); t["place_ms"] = best[2]
    else:
        t["place_ms"] = None

raw = defaultdict(list)
for t in trades:
    t0 = t["place_ms"] or t["bar_ms"]
    raw[t["sym"]] += [(t0, t0 + WMAX_MS), (t["close_ms"], t["close_ms"] + WMAX_MS)]
win_s, win_e = {}, {}
for sym, ws in raw.items():
    ws.sort(); merged = [list(ws[0])]
    for a, b in ws[1:]:
        if a <= merged[-1][1]: merged[-1][1] = max(merged[-1][1], b)
        else: merged.append([a, b])
    win_s[sym] = [m[0] for m in merged]; win_e[sym] = [m[1] for m in merged]

kept = defaultdict(list); tape_min = tape_max = None
for tf in sorted(glob.glob(TAPE_GLOB)):
    op = gzip.open if tf.endswith(".gz") else open
    try:
        with op(tf, "rt") as f:
            for r in csv.reader(f):
                if not r or r[0] == "time_ms": continue
                try: tt = int(r[0]); sym = r[1]; side = r[2]; px = float(r[3])
                except Exception: continue
                tape_min = tt if tape_min is None else min(tape_min, tt)
                tape_max = tt if tape_max is None else max(tape_max, tt)
                ss = win_s.get(sym)
                if ss is None: continue
                i = bisect.bisect_right(ss, tt) - 1
                if i >= 0 and tt <= win_e[sym][i]: kept[sym].append((tt, side, px))
    except Exception as e:
        print(f"WARN {tf}: {e}")
for sym in kept: kept[sym].sort()
if tape_min is None:
    print("no tape found"); sys.exit(0)


def fill_time(sym, level, is_sell, t0):
    """ms after t0 at which a resting order first prints THROUGH, else None."""
    rows = kept.get(sym)
    if not rows: return "?"
    lo = bisect.bisect_left(rows, (t0,)); tend = t0 + WMAX_MS
    for j in range(lo, len(rows)):
        tt, side, px = rows[j]
        if tt > tend: break
        if is_sell and side == "B" and px > level: return tt - t0
        if (not is_sell) and side == "A" and px < level: return tt - t0
    return None


def taker_px(sym, t_at, want_sell):
    rows = kept.get(sym)
    if not rows: return None
    want = "A" if want_sell else "B"
    for j in range(bisect.bisect_right(rows, (t_at, chr(255), 0)) - 1, -1, -1):
        tt, side, px = rows[j]
        if tt < t_at - STALE_MS: return None
        if side == want: return px
    return None


# ---- per trade: fill latency + taker price at every candidate W ----
for t in trades:
    t["ok"] = False
    t0 = t["place_ms"]
    if t0 is None: continue
    if not (tape_min <= t0 and t["close_ms"] + WMAX_MS <= tape_max): continue
    is_sell = t["side"] == "SHORT"
    ft = fill_time(t["sym"], t["entry_px"], is_sell, t0)
    if ft == "?": continue
    t["fill_ms"] = ft
    t["tpx"] = {w: taker_px(t["sym"], t0 + w * 1000, is_sell) for w in WINDOWS}
    if any(v is None or v <= 0 for v in t["tpx"].values()): continue
    t["ok"] = True

univ = [t for t in trades if t["ok"]]
print(f"tape {datetime.fromtimestamp(tape_min/1000, tz=timezone.utc):%Y-%m-%d %H:%M} -> "
      f"{datetime.fromtimestamp(tape_max/1000, tz=timezone.utc):%Y-%m-%d %H:%M}   "
      f"common measurable trade set: {len(univ)} of {len(trades)}")
print("(a trade is in the set only if its fill latency AND a taker price at every W are "
      "measurable, so all columns below score the SAME trades)")


def score(ts, w, exit_bps):
    """(abandon_pnl, cross_pnl, n_filled, n_crossed)"""
    ab = cr = 0.0; nf = nc = 0
    for t in ts:
        if t["fill_ms"] is not None and t["fill_ms"] <= w * 1000:
            ab += t["pnl"]; cr += t["pnl"]; nf += 1          # maker fill, both policies
        else:
            nc += 1                                          # ABANDON takes nothing
            tpx = t["tpx"][w]
            d = 1 if t["side"] == "LONG" else -1
            gross = d * (t["exit_px"] - tpx) / tpx
            cr += t["notional"] * (gross - (TAKER_BPS + exit_bps) / 1e4)
    return ab, cr, nf, nc


for label, ts in (("ALL ARMS", univ),
                  ("15m-ats only", [t for t in univ if t["arm"] == "paper_15m_ats"])):
    if not ts: continue
    print(f"\n=== {label}  (n={len(ts)}) ===")
    hdr = (f"{'timeout W':>9} {'maker-filled':>13} {'crossed':>8} | {'ABANDON$':>9} | "
           f"{'CROSS$ (mk exit)':>17} {'CROSS$ (tk exit)':>17} | {'better?':>8}")
    print(hdr); print("-" * len(hdr))
    for w in WINDOWS:
        ab, cr_mk, nf, nc = score(ts, w, MAKER_BPS)
        _, cr_tk, _, _ = score(ts, w, TAKER_BPS)
        flag = "CROSS" if cr_tk > ab else ("cross(mk)" if cr_mk > ab else "abandon")
        print(f"{w:>8}s {nf:>13} {nc:>8} | {ab:>+9.2f} | {cr_mk:>+17.2f} {cr_tk:>+17.2f} | {flag:>8}")
    # marginal value of the fills each extra second of patience buys. Adverse selection
    # predicts this decays: the longer it takes to fill, the more the fill means price
    # came BACK to you, i.e. the fade had not started.
    print(f"  {'fill latency':>14} {'n':>4} {'booked $':>9} {'$/trade':>9}")
    edges = [0, 30, 60, 120, 300, 600, 900]
    for a, b in zip(edges, edges[1:]):
        g = [t for t in ts if t["fill_ms"] is not None and a*1000 < t["fill_ms"] <= b*1000]
        if g:
            s = sum(t["pnl"] for t in g)
            print(f"  {f'{a}-{b}s':>14} {len(g):>4} {s:>+9.2f} {s/len(g):>+9.3f}")
    nev = [t for t in ts if t["fill_ms"] is None or t["fill_ms"] > 900*1000]
    if nev:
        s = sum(t["pnl"] for t in nev)
        print(f"  {'never (>900s)':>14} {len(nev):>4} {s:>+9.2f} {s/len(nev):>+9.3f}"
              f"   <- the alpha you cannot reach")
    best_ab = max(WINDOWS, key=lambda w: score(ts, w, MAKER_BPS)[0])
    best_cr = max(WINDOWS, key=lambda w: score(ts, w, TAKER_BPS)[1])
    ab_b = score(ts, best_ab, MAKER_BPS)[0]
    cr_b = score(ts, best_cr, TAKER_BPS)[1]
    print(f"  best ABANDON: W={best_ab}s -> ${ab_b:+.2f}     "
          f"best CROSS (taker exit): W={best_cr}s -> ${cr_b:+.2f}")
    print(f"  edge from adding a taker fallback: ${cr_b - ab_b:+.2f} "
          f"over {len(ts)} trades = ${(cr_b - ab_b)/len(ts):+.3f}/trade")
