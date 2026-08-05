#!/usr/bin/env python3
"""Would a SHORTER backstop have helped? Reconstructed from the real live fills.

The live book decomposes cleanly: 132 reclaim exits earn +112 bps, 42 backstop exits
lose -247 bps, and the backstop group is the entire loss book ($-36.76 of $-45.95 gross
losses). take_profit.py already showed capping WINNERS does not touch this, and
wide_stop.py showed price stops kill the reversion the strategy exists to capture.

The untested lever is TIME. A trade that has not reclaimed by hour 4 might already be
telling you it will not reclaim -- and if the average backstop loser keeps bleeding from
hour 4 to hour 8, cutting early is free money. If instead the loss is already fully
realised by hour 4 and the position is just sitting there, cutting early saves nothing
and forfeits the late reclaims.

Method: fetch 15m candles covering each trade, walk the path, and re-exit every trade at
a shorter horizon H. Trades that really exited before H keep their real net_bps (nothing
changes for them). Trades still open at H are marked out at that bar's close and charged
a taker exit. No lookahead -- the rule only needs a clock.

  python3 analysis/live_path.py [trades.csv]
"""
import csv, json, math, sys, time, urllib.request
from datetime import datetime, timezone

PATH = sys.argv[1] if len(sys.argv) > 1 else "live_15m_ats/trades_15m.csv"
TAKER_BPS = 4.5          # forced exit crosses: ~3.5bps fee + slip, matches observed
CUTOFFS = [1, 2, 3, 4, 5, 6, 7, 8]


def post(body, tries=6):
    for k in range(tries):
        try:
            req = urllib.request.Request("https://api.hyperliquid.xyz/info",
                                         data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            time.sleep(min(20, 2 ** k))
    return None


def pms(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


rows = []
for r in csv.DictReader(open(PATH)):
    try:
        net = float(r["net_bps"])
        if abs(net) < 1e-9 or r["reason"] in ("closed_externally",):
            continue
        rows.append(dict(sym=r["symbol"], side=r["side"], net=net,
                         pnl=float(r["pnl_usd"]), reason=r["reason"],
                         entry=float(r["entry_px"]), hold=float(r["hold_h"]),
                         t=pms(r["entry_time"]), ct=pms(r["close_time"]),
                         ntl=abs(float(r["pnl_usd"]) / (net / 1e4))))
    except Exception:
        pass
print(f"loaded {len(rows)} trades from {PATH}")

syms = sorted({r["sym"] for r in rows})
lo = min(r["t"] for r in rows) - 3600000
hi = max(r["ct"] for r in rows) + 12 * 3600000
print(f"fetching 15m candles for {len(syms)} coins ...")
cd = {}
for s in syms:
    d = post({"type": "candleSnapshot",
              "req": {"coin": s, "interval": "15m", "startTime": lo, "endTime": hi}})
    if d:
        try:
            cd[s] = sorted((int(c["T"]), float(c["c"]), float(c["h"]), float(c["l"]))
                           for c in d)
        except Exception:
            pass
    time.sleep(0.03)
print(f"got {len(cd)}\n")


def mark(r, at_ms):
    """close-to-close return in bps at the last candle closing at or before at_ms"""
    v = cd.get(r["sym"])
    if not v:
        return None
    px = None
    for t, c, h, l in v:
        if t <= at_ms:
            px = c
        else:
            break
    if px is None:
        return None
    d = -1.0 if r["side"] == "SHORT" else 1.0
    return d * (px - r["entry"]) / r["entry"] * 1e4


def st(v):
    n = len(v)
    if n < 2:
        return (float("nan"), float("nan"), n)
    mu = sum(v) / n
    sd = (sum((x - mu) ** 2 for x in v) / (n - 1)) ** 0.5
    return (mu, mu / (sd / math.sqrt(n)) if sd > 0 else float("nan"), n)


# ---------- 1. how does the loser's loss evolve? ----------
print("=== 1. PATH OF THE BACKSTOP LOSERS: is the loss still growing after hour 4? ===")
bs = [r for r in rows if r["reason"].startswith("backstop")]
rc = [r for r in rows if r["reason"] == "reclaim"]
print(f"  {'hour':>5} | {'backstop trades (n=%d)' % len(bs):>26} | "
      f"{'reclaim trades (n=%d)' % len(rc):>26}")
print(f"  {'':>5} | {'mark bps':>12} {'still open':>13} | {'mark bps':>12} {'still open':>13}")
for H in CUTOFFS:
    out = [f"  {H:>5} |"]
    for seg in (bs, rc):
        ms = [mark(r, r["t"] + H * 3600000) for r in seg]
        ms = [m for m in ms if m is not None]
        openn = sum(1 for r in seg if r["hold"] > H)
        mu = sum(ms) / len(ms) if ms else float("nan")
        out.append(f" {mu:>+12.1f} {openn:>8}/{len(seg):<4} |")
    print("".join(out))
print("  A loss that is flat from hour 4 to hour 8 means cutting early saves nothing;")
print("  a loss that keeps deepening means the backstop is holding a losing position too long.\n")

# ---------- 2. re-exit everything at a shorter horizon ----------
print("=== 2. SHORTER BACKSTOP, applied causally to every trade ===")
print(f"  {'cutoff':>7} {'n cut':>6} {'mean bps':>10} {'t':>6} {'win%':>6} "
      f"{'total $':>9} {'vs live':>9} {'sharpe':>7}")


def run(H):
    out = []
    for r in rows:
        if r["hold"] <= H:
            out.append((r["net"], r["ntl"], False))
            continue
        m = mark(r, r["t"] + H * 3600000)
        if m is None:                       # no candle: leave the trade as it was
            out.append((r["net"], r["ntl"], False))
            continue
        out.append((m - TAKER_BPS, r["ntl"], True))
    return out


live_tot = sum(r["pnl"] for r in rows)
mu0, t0, _ = st([r["net"] for r in rows])
sd0 = (sum((r["net"] - mu0) ** 2 for r in rows) / (len(rows) - 1)) ** 0.5
print(f"  {'live 8h':>7} {0:>6} {mu0:>+10.1f} {t0:>+6.1f} "
      f"{100*sum(1 for r in rows if r['net']>0)/len(rows):>5.0f}% "
      f"{live_tot:>+9.2f} {0.0:>+9.2f} {mu0/sd0:>7.3f}")
for H in CUTOFFS:
    res = run(H)
    b = [x[0] for x in res]
    mu, t, n = st(b)
    sd = (sum((x - mu) ** 2 for x in b) / (n - 1)) ** 0.5
    tot = sum(x[0] / 1e4 * x[1] for x in res)
    print(f"  {H:>6}h {sum(1 for x in res if x[2]):>6} {mu:>+10.1f} {t:>+6.1f} "
          f"{100*sum(1 for x in b if x>0)/n:>5.0f}% {tot:>+9.2f} {tot-live_tot:>+9.2f} "
          f"{mu/sd:>7.3f}")

# ---------- 3. what does the cut actually trade away? ----------
print("\n=== 3. DECOMPOSITION at each cutoff: what is gained, what is forfeited ===")
print(f"  {'cutoff':>7} {'losers cut':>28} {'winners cut short':>30}")
for H in CUTOFFS:
    gain = loss = 0.0
    ng = nl = 0
    for r in rows:
        if r["hold"] <= H:
            continue
        m = mark(r, r["t"] + H * 3600000)
        if m is None:
            continue
        delta = (m - TAKER_BPS - r["net"]) / 1e4 * r["ntl"]
        if r["net"] < 0:
            gain += delta; ng += 1
        else:
            loss += delta; nl += 1
    print(f"  {H:>6}h   n={ng:<3} ${gain:>+7.2f} saved on losers   "
          f"n={nl:<3} ${loss:>+7.2f} on trades that were winning")

# ---------- 4. is the reclaim rate still worth waiting for, hour by hour? ----------
print("\n=== 4. HAZARD: of trades still open at hour H, what happens next? ===")
print(f"  {'hour':>5} {'still open':>11} {'reclaim later':>14} {'-> backstop':>12} "
      f"{'mean bps from here':>19}")
for H in CUTOFFS:
    live_open = [r for r in rows if r["hold"] > H]
    if not live_open:
        continue
    later_rc = [r for r in live_open if r["reason"] == "reclaim"]
    fwd = []
    for r in live_open:
        m = mark(r, r["t"] + H * 3600000)
        if m is not None:
            fwd.append(r["net"] - m)
    mu, t, n = st(fwd) if fwd else (float("nan"),) * 3
    print(f"  {H:>5} {len(live_open):>11} {len(later_rc):>14} "
          f"{len(live_open)-len(later_rc):>12} {mu:>+15.1f} (t={t:+.1f})")
print("  'mean bps from here' is the forward return of holding on past hour H.")
print("  If it is positive, waiting is still paying; if negative, the backstop is too late.")
