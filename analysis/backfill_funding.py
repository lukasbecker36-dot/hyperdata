#!/usr/bin/env python3
"""One-off: add historical crypto funding to cum_pnl so the reported P&L is total return.

The trade log is built from fills and closedPnl excludes funding, so every P&L figure the
bot has written is price-only. This adds the funding actually received since the arm went
live, recorded as a separate state field so the adjustment stays auditable rather than
being silently folded into a number that no longer reconciles to the trade rows.

Only CRYPTO funding is counted. The account also carries hyperaster's xyz: equity perps,
which are not this bot's.
"""
import json, sys, time, urllib.request
from datetime import datetime, timezone

ADDR = "0x269eB9Ac8e342f58fE4F56f5d3BDCC03EFd5B3C5"
STATE = "/opt/hyperdata/live_15m_ats/state_15m.json"
SINCE = "2026-07-25"


def post(b):
    r = urllib.request.Request("https://api.hyperliquid.xyz/info",
                               data=json.dumps(b).encode(),
                               headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=30))


start = int(datetime.strptime(SINCE, "%Y-%m-%d")
            .replace(tzinfo=timezone.utc).timestamp() * 1000)
raw, cur = [], start
while True:
    b = post({"type": "userFunding", "user": ADDR, "startTime": cur})
    if not b:
        break
    raw += b
    if len(b) < 500:
        break
    cur = b[-1]["time"] + 1
seen, u = set(), []
for x in raw:
    k = (x["time"], x["delta"].get("coin"), x["delta"].get("usdc"))
    if k not in seen:
        seen.add(k); u.append(x)
cry = [x for x in u
       if ":" not in x["delta"]["coin"] and not x["delta"]["coin"].startswith("@")]
total = sum(float(x["delta"]["usdc"]) for x in cry)
xyz = sum(float(x["delta"]["usdc"]) for x in u if x not in cry)
print(f"crypto funding since {SINCE}: {len(cry)} settlements, ${total:+.4f}")
print(f"excluded xyz/equity (hyperaster): ${xyz:+.4f}")

st = json.load(open(STATE))
prev = st.get("funding_backfill")
if prev is not None:
    print(f"ALREADY APPLIED (${prev:+.4f}) — not double-counting. Exiting.")
    sys.exit(0)
before = st["cum_pnl"]
st["funding_backfill"] = round(total, 6)
st["cum_pnl"] = round(before + total, 6)
json.dump(st, open(STATE, "w"), indent=1)
print(f"cum_pnl {before:+.4f} -> {st['cum_pnl']:+.4f}   (funding_backfill recorded)")
