#!/usr/bin/env python3
"""Reconstruct the tier each traded coin was in AT THE TIME, and validate it.

Classifying with today's volumes is wrong: ~10% of names cross a tertile boundary within
days (19 of paper_15m's 180 trades classify as LOW now, in a HIGH+MID-only arm). That
mislabelling is why the tier analyses disagreed with each other.

The bot assigned tiers from metaAndAssetCtxs.dayNtlVlm -- a rolling 24h notional -- and
split the active universe into tertiles. Both halves are reconstructible:

  volume  sum(volume x close) over the trailing 96 15m bars, from candleSnapshot, which
          is what a rolling 24h notional is
  bounds  recompute the tertiles across all active perps at that moment

and crucially it is CHECKABLE: the bot logged "universe: N active perps (tier bounds
$A / $B)" every time it loaded the universe. A correct reconstruction has to reproduce
those numbers. If it does not, the reconstruction is wrong and nothing built on it counts.

  python3 analysis/tier_history.py [logfile] [days_back]
"""
import json, math, re, sys, time, urllib.request
from datetime import datetime, timezone

LOG = sys.argv[1] if len(sys.argv) > 1 else "live_15m_ats/bot_15m.log"
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 14
WIN = 96                       # 96 x 15m = 24h
BAR_MS = 15 * 60 * 1000

BOUND_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+universe: (\d+) active perps"
                      r"\s+\(tier bounds \$([\d,]+) / \$([\d,]+)\)")


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


# ---- the logged bounds we must reproduce ----
logged = []
try:
    for line in open(LOG, errors="replace"):
        m = BOUND_RE.match(line)
        if m:
            logged.append((pms(m.group(1)), int(m.group(2)),
                           float(m.group(3).replace(",", "")),
                           float(m.group(4).replace(",", ""))))
except Exception as e:
    print(f"could not read {LOG}: {e}")
print(f"logged universe loads found: {len(logged)}")
if not logged:
    print("nothing to validate against"); sys.exit(0)

m = post({"type": "metaAndAssetCtxs"})
names = [u["name"] for u, c in zip(m[0]["universe"], m[1]) if c.get("midPx") is not None]
print(f"active perps now: {len(names)}   fetching {DAYS}d of 15m candles ...")

end = int(time.time() * 1000)
start = end - DAYS * 86400000
vol = {}                        # sym -> [(bar_ms, notional_of_that_bar)]
for i, s in enumerate(names):
    d = post({"type": "candleSnapshot",
              "req": {"coin": s, "interval": "15m", "startTime": start, "endTime": end}})
    if not d:
        continue
    try:
        vol[s] = [(int(c["T"]), float(c["v"]) * float(c["c"])) for c in d]
    except Exception:
        pass
    if (i + 1) % 40 == 0:
        print(f"  {i+1}/{len(names)}")
    time.sleep(0.03)
print(f"got candles for {len(vol)} coins\n")


def day_ntl(sym, at_ms):
    """Rolling 24h notional as of at_ms -- the quantity the bot tiered on."""
    v = vol.get(sym)
    if not v:
        return None
    tot = 0.0
    n = 0
    for t, x in v:
        if at_ms - 86400000 <= t < at_ms:
            tot += x; n += 1
    return tot if n >= WIN // 2 else None


def bounds_at(at_ms):
    vals = sorted(x for x in (day_ntl(s, at_ms) for s in vol) if x)
    if len(vals) < 30:
        return None
    return (vals[len(vals)//3], vals[2*len(vals)//3], len(vals))


print("=== VALIDATION: reconstructed tertiles vs what the bot logged ===")
print(f"  {'when':>17} {'logged q1':>12} {'mine q1':>12} {'err':>7} "
      f"{'logged q2':>12} {'mine q2':>12} {'err':>7}")
errs = []
for t, n, a, b in logged[-12:]:
    got = bounds_at(t)
    if not got:
        print(f"  {datetime.fromtimestamp(t/1000, tz=timezone.utc):%Y-%m-%d %H:%M}"
              f"   no candle coverage"); continue
    q1, q2, cnt = got
    e1, e2 = (q1-a)/a*100, (q2-b)/b*100
    errs += [abs(e1), abs(e2)]
    print(f"  {datetime.fromtimestamp(t/1000, tz=timezone.utc):%Y-%m-%d %H:%M} "
          f"{a:>12,.0f} {q1:>12,.0f} {e1:>+6.0f}% {b:>12,.0f} {q2:>12,.0f} {e2:>+6.0f}%")
if errs:
    med = sorted(errs)[len(errs)//2]
    print(f"\n  median absolute error: {med:.0f}%")
    if med < 15:
        print("  -> reconstruction tracks the bot's own bounds; historical tiers are usable")
    else:
        print("  -> TOO FAR OFF. Do not build tier analysis on this; wait for the live")
        print("     `tier` column instead, which records the bot's own classification.")
