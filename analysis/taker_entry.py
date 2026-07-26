#!/usr/bin/env python3
"""Would a TAKER entry on maker-timeout be worth it?

The shadow-fill audit found the entries that never filled were disproportionately
WINNERS (filtering to filled-only cut booked P&L from +$111 to +$59). That is maker
adverse selection: a resting sell only gets lifted while buyers are still coming, so
you win the fills where the fade has not started and miss the ones where price walked
away in your favour immediately.

So the missed trades hold alpha. The catch is you cannot have it at the resting price —
by the timeout the price has already moved away, which is *why* you did not fill. This
script prices that honestly:

  for every entry that did NOT fill within W seconds, take it as a taker at the timeout
  and recompute the trade.

The taker price comes straight off the tape's aggressor side, so the spread is included
by construction rather than assumed:
  we need to SELL (short entry) -> we hit the bid  -> price of the last 'A' print
  we need to BUY  (long  entry) -> we lift the ask -> price of the last 'B' print

Assumptions, stated because they flatter the result:
  - exit price is taken unchanged from the actual trade. The reclaim exit is triggered by
    bar closes, independent of our entry, so this is fair; the 8h backstop would shift by
    the ~W-second entry delay, which is negligible.
  - the entry is assumed to fill in full at the timeout. Real taker fills walk the book.
  - fees: taker 4.5bps on the entry. Exit is priced both ways (maker 1.5 / taker 4.5)
    because the audit found ~25% of exits do not fill passively either.

Run on the server (the tape stays local):
  python3 analysis/taker_entry.py [trades_glob]
"""
import csv, gzip, glob, os, bisect, re, sys
from collections import defaultdict
from datetime import datetime, timezone

TRADES_GLOB = sys.argv[1] if len(sys.argv) > 1 else "live/*.csv"
TAPE_GLOB   = "tape/tape_*.csv*"
LOG_GLOB    = "{arm}/bot_*.log"
W           = int(sys.argv[2]) if len(sys.argv) > 2 else 300   # maker entry window (s)
WMAX_MS     = 900 * 1000
STALE_MS    = 60 * 1000    # how far back we will reach for a taker price at the timeout
MAKER_BPS   = 1.5
TAKER_BPS   = 4.5
MAX_PLACE_DELAY_S = 1800

OPEN_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+OPEN\s+(\S+)\s+(SHORT|LONG)\s+@\s+(\S+)")


def pms(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


# ---- load trades ----
trades = []
for path in sorted(glob.glob(TRADES_GLOB)):
    if "shadow_fill" in os.path.basename(path) or "taker" in os.path.basename(path):
        continue
    arm = os.path.basename(path).replace(".csv", "")
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                net = float(r["net_bps"]) / 1e4
                pnl = float(r["pnl_usd"])
                ntl = pnl / net if abs(net) > 1e-12 else None
                trades.append(dict(
                    arm=arm, sym=r["symbol"], side=r["side"],
                    bar_ms=pms(r["entry_time"]), close_ms=pms(r["close_time"]),
                    entry_px=float(r["entry_px"]), exit_px=float(r["exit_px"]),
                    pnl=pnl, net_bps=float(r["net_bps"]), notional=ntl,
                    reason=r["reason"]))
            except Exception:
                pass

# ---- recover placement times from the bot logs ----
opens = defaultdict(list)
for arm in sorted({t["arm"] for t in trades}):
    for lf in sorted(glob.glob(LOG_GLOB.format(arm=arm))):
        try:
            with open(lf, errors="replace") as f:
                for line in f:
                    m = OPEN_RE.match(line)
                    if not m: continue
                    ts, sym, side, px = m.groups()
                    try: pxf = float(px)
                    except ValueError: continue
                    opens[(arm, sym, side)].append((pms(ts), pxf))
        except Exception as e:
            print(f"WARN {lf}: {e}")
for k in opens:
    opens[k].sort()

used = defaultdict(set)
for t in sorted(trades, key=lambda x: (x["arm"], x["sym"], x["bar_ms"])):
    cand = opens.get((t["arm"], t["sym"], t["side"]), [])
    best = None
    for i, (ts, pxf) in enumerate(cand):
        if i in used[(t["arm"], t["sym"], t["side"])]: continue
        d = ts - t["bar_ms"]
        if d < 0 or d > MAX_PLACE_DELAY_S * 1000: continue
        ok = abs(pxf - t["entry_px"]) <= 1e-4 * max(abs(t["entry_px"]), 1e-12)
        key = (0 if ok else 1, d)
        if best is None or key < best[0]: best = (key, i, ts)
    if best is not None:
        used[(t["arm"], t["sym"], t["side"])].add(best[1])
        t["place_ms"] = best[2]
    else:
        t["place_ms"] = None

# ---- stream the tape ----
raw = defaultdict(list)
for t in trades:
    t0 = t["place_ms"] if t["place_ms"] is not None else t["bar_ms"]
    raw[t["sym"]].append((t0, t0 + WMAX_MS))
    raw[t["sym"]].append((t["close_ms"], t["close_ms"] + WMAX_MS))
win_s, win_e = {}, {}
for sym, ws in raw.items():
    ws.sort(); merged = [list(ws[0])]
    for a, b in ws[1:]:
        if a <= merged[-1][1]: merged[-1][1] = max(merged[-1][1], b)
        else: merged.append([a, b])
    win_s[sym] = [m[0] for m in merged]; win_e[sym] = [m[1] for m in merged]

kept = defaultdict(list)
tape_min = tape_max = None
for tf in sorted(glob.glob(TAPE_GLOB)):
    op = gzip.open if tf.endswith(".gz") else open
    try:
        with op(tf, "rt") as f:
            for r in csv.reader(f):
                if not r or r[0] == "time_ms": continue
                try:
                    tt = int(r[0]); sym = r[1]; side = r[2]; px = float(r[3])
                except Exception:
                    continue
                tape_min = tt if tape_min is None else min(tape_min, tt)
                tape_max = tt if tape_max is None else max(tape_max, tt)
                ss = win_s.get(sym)
                if ss is None: continue
                i = bisect.bisect_right(ss, tt) - 1
                if i >= 0 and tt <= win_e[sym][i]:
                    kept[sym].append((tt, side, px))
    except Exception as e:
        print(f"WARN {tf}: {e}")
for sym in kept:
    kept[sym].sort()
if tape_min is None:
    print("no tape found"); sys.exit(0)


def through(sym, level, is_sell, t0, w):
    """Did an opposite aggressor print THROUGH our resting level in [t0, t0+w]?"""
    rows = kept.get(sym)
    if not rows: return None                       # unmeasurable
    lo = bisect.bisect_left(rows, (t0,)); tend = t0 + w * 1000
    for j in range(lo, len(rows)):
        tt, side, px = rows[j]
        if tt > tend: break
        if is_sell and side == "B" and px > level: return True
        if (not is_sell) and side == "A" and px < level: return True
    return False


def taker_px(sym, t_at, want_sell):
    """Price we'd pay crossing at t_at. Selling hits the bid ('A' prints);
    buying lifts the ask ('B' prints). Spread is included by construction."""
    rows = kept.get(sym)
    if not rows: return None
    want = "A" if want_sell else "B"
    hi = bisect.bisect_right(rows, (t_at, chr(255), 0)) - 1
    for j in range(hi, -1, -1):
        tt, side, px = rows[j]
        if tt < t_at - STALE_MS: return None
        if side == want: return px
    return None


# ---- evaluate ----
res = defaultdict(lambda: {"missed": 0, "measurable": 0, "booked": 0.0,
                           "taker_mk": 0.0, "taker_tk": 0.0, "worse": 0, "better": 0,
                           "drift": []})
skipped = defaultdict(int)
for t in trades:
    t0 = t["place_ms"]
    if t0 is None or t["notional"] is None:
        skipped["no placement time / notional"] += 1; continue
    if not (tape_min <= t0 and t["close_ms"] + WMAX_MS <= tape_max):
        skipped["outside tape coverage"] += 1; continue
    is_sell = t["side"] == "SHORT"                 # short entry rests as a SELL
    f = through(t["sym"], t["entry_px"], is_sell, t0, W)
    if f is None:
        skipped["coin absent from tape"] += 1; continue
    if f:
        continue                                   # it filled as a maker; not our question
    r = res[t["arm"]]
    r["missed"] += 1
    tpx = taker_px(t["sym"], t0 + W * 1000, is_sell)
    if tpx is None or tpx <= 0:
        skipped["no taker price at timeout"] += 1; continue
    r["measurable"] += 1
    r["booked"] += t["pnl"]                        # what the paper arm booked (fantasy price)
    d = 1 if t["side"] == "LONG" else -1
    gross = d * (t["exit_px"] - tpx) / tpx
    # how much worse the taker entry is than the resting price we never got
    r["drift"].append(d * (tpx - t["entry_px"]) / t["entry_px"] * 1e4)
    for key, exit_fee in (("taker_mk", MAKER_BPS), ("taker_tk", TAKER_BPS)):
        net = gross - (TAKER_BPS + exit_fee) / 1e4
        r[key] += t["notional"] * net
    net_mk = gross - (TAKER_BPS + MAKER_BPS) / 1e4
    if t["notional"] * net_mk > 0: r["better"] += 1
    else: r["worse"] += 1

print(f"Taker-entry-on-timeout test   W={W}s   fees: entry {TAKER_BPS}bps taker, "
      f"exit {MAKER_BPS}/{TAKER_BPS}bps")
print(f"tape: {datetime.fromtimestamp(tape_min/1000, tz=timezone.utc):%Y-%m-%d %H:%M} -> "
      f"{datetime.fromtimestamp(tape_max/1000, tz=timezone.utc):%Y-%m-%d %H:%M}")
if skipped:
    print("skipped: " + ", ".join(f"{v} {k}" for k, v in sorted(skipped.items())))
print()
hdr = (f"{'arm':20s} {'missed':>7} {'meas':>5} {'booked$':>9} {'taker$':>9} "
       f"{'taker$':>9} {'win%':>6} {'med drift':>10}")
print(hdr)
print(f"{'':20s} {'':>7} {'':>5} {'(fantasy)':>9} {'(mk exit)':>9} {'(tk exit)':>9} "
      f"{'':>6} {'bps vs rest':>10}")
print("-" * len(hdr))
T = defaultdict(float); TM = TB = TW = 0
for arm, r in sorted(res.items(), key=lambda x: -x[1]["taker_mk"]):
    if not r["measurable"]: continue
    dr = sorted(r["drift"]); med = dr[len(dr)//2]
    wr = r["better"] / max(1, r["better"] + r["worse"]) * 100
    print(f"{arm:20s} {r['missed']:>7} {r['measurable']:>5} {r['booked']:>+9.2f} "
          f"{r['taker_mk']:>+9.2f} {r['taker_tk']:>+9.2f} {wr:>5.0f}% {med:>+10.1f}")
    T["booked"] += r["booked"]; T["mk"] += r["taker_mk"]; T["tk"] += r["taker_tk"]
    TM += r["measurable"]; TB += r["better"]; TW += r["worse"]
    T["drift"] = T.get("drift", []) or []
print("-" * len(hdr))
alld = sorted(d for r in res.values() for d in r["drift"])
print(f"{'TOTAL':20s} {sum(r['missed'] for r in res.values()):>7} {TM:>5} "
      f"{T['booked']:>+9.2f} {T['mk']:>+9.2f} {T['tk']:>+9.2f} "
      f"{TB/max(1,TB+TW)*100:>5.0f}% {(alld[len(alld)//2] if alld else 0):>+10.1f}")
print()
print(f"per missed trade: booked (fantasy) ${T['booked']/max(1,TM):+.3f}  ->  "
      f"taker ${T['mk']/max(1,TM):+.3f} (maker exit) / ${T['tk']/max(1,TM):+.3f} (taker exit)")
print(f"median adverse drift from resting price to taker price: "
      f"{(alld[len(alld)//2] if alld else 0):+.1f} bps")
print()
if T["mk"] > 0 and T["tk"] > 0:
    print("VERDICT: taker-on-timeout is positive under both exit assumptions -> worth adding.")
elif T["mk"] > 0:
    print("VERDICT: positive ONLY if the exit fills passively. Marginal — the exit that")
    print("         does not fill is exactly the case where this loses money.")
else:
    print("VERDICT: negative. The drift that stopped the maker fill has already eaten the")
    print("         edge; crossing to chase it pays spread + taker fee for a worse entry.")
