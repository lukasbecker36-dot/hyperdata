#!/usr/bin/env python3
"""What the CASHCAT trade would have done at the leverage the bot now uses.

The -1489bps CASHCAT loss is the single largest item in the live book -- 39% of gross
profit destroyed by one trade. It was a LIQUIDATION at 3x. CASHCAT's maxLeverage is 3,
so maintenance is 1/6, and because a short's position value grows as it moves against
you the liquidation point is 14.29%, not the naive 1/L - mm = 16.67%. The adverse move
was 14.83%. It cleared the trigger by half a percent.

_lev_for() now sizes CASHCAT at 1x, where liquidation needs +71.4%. So this trade could
not happen again -- it was a leverage error, not a strategy error.

But that does NOT make the trade free. It would still have been open, still moving
against us, and would have exited at the 8h backstop or on a reclaim. Deleting it from
the sample flatters the strategy; the honest correction is to REPLACE its outcome with
what the position would actually have returned. That is what this computes, by walking
the real 15m candles under the bot's own exit rule.

Leverage is irrelevant to P&L except through liquidation -- notional is $35 either way --
so this is the only trade in the 177 whose outcome the leverage fix changes.

  python3 analysis/cashcat_counterfactual.py
"""
import json, math, time, urllib.request
from datetime import datetime, timezone

SYM, SIDE = "CASHCAT", "SHORT"
ENTRY_PX, NOTIONAL = 0.07924, 34.94
ENTRY = "2026-08-04 23:16:28"
BACKSTOP_BARS, WIN = 32, 96          # 8h hold, 24h lookback, on 15m bars
EXIT_COST_BPS = 6.4                  # the fee actually charged on the real exit


def post(b, tries=6):
    for k in range(tries):
        try:
            r = urllib.request.Request("https://api.hyperliquid.xyz/info",
                                       data=json.dumps(b).encode(),
                                       headers={"Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(r, timeout=30))
        except Exception:
            time.sleep(min(20, 2 ** k))
    return None


t0 = int(datetime.strptime(ENTRY, "%Y-%m-%d %H:%M:%S")
         .replace(tzinfo=timezone.utc).timestamp() * 1000)
lo = t0 - (WIN + 4) * 900000
hi = t0 + (BACKSTOP_BARS + 8) * 900000
d = post({"type": "candleSnapshot",
          "req": {"coin": SYM, "interval": "15m", "startTime": lo, "endTime": hi}})
c = sorted((int(x["T"]), float(x["o"]), float(x["h"]), float(x["l"]), float(x["c"]))
           for x in d)
sig = max(i for i, x in enumerate(c) if x[0] <= t0)      # the bar we entered on
print(f"{SYM} {SIDE}  entry {ENTRY}  @ {ENTRY_PX}  notional ${NOTIONAL:.2f}")
print(f"candles: {len(c)} bars, signal bar index {sig}\n")

# the reclaim level: a fade-short exits when price closes back inside the prior 24h range
prior = c[sig - WIN:sig]
ph = max(x[2] for x in prior)
print(f"prior 24h high (the reclaim level) = {ph:.6f}   entry was {ENTRY_PX:.6f} "
      f"({(ENTRY_PX/ph-1)*100:+.1f}% vs it)\n")

print("=== the path, bar by bar ===")
print(f"  {'hour':>5} {'high':>10} {'close':>10} {'adverse %':>10} {'P&L bps':>9}  note")
mae = 0.0
exit_i = exit_px = exit_why = None
for k in range(1, BACKSTOP_BARS + 1):
    if sig + k >= len(c):
        break
    _, _, h, l, cl = c[sig + k]
    adv = (h - ENTRY_PX) / ENTRY_PX
    mae = max(mae, adv)
    pnl = -(cl - ENTRY_PX) / ENTRY_PX * 1e4
    note = ""
    if exit_i is None and cl < ph:
        exit_i, exit_px, exit_why = k, cl, "reclaim"
        note = "<-- RECLAIM, would have exited here"
    if k % 4 == 0 or note or k <= 2:
        print(f"  {k*0.25:>5.1f} {h:>10.6f} {cl:>10.6f} {adv*100:>+9.2f}% "
              f"{pnl:>+9.1f}  {note}")
    if exit_i is not None:
        break
if exit_i is None:
    kk = min(BACKSTOP_BARS, len(c) - sig - 1)
    exit_i, exit_px, exit_why = kk, c[sig + kk][4], "backstop"
    print(f"  -> never reclaimed; 8h backstop at bar {kk}, px {exit_px:.6f}")

gross = -(exit_px - ENTRY_PX) / ENTRY_PX * 1e4
net = gross - EXIT_COST_BPS
usd = net / 1e4 * NOTIONAL
print(f"\n=== counterfactual at 1x ===")
print(f"  max adverse excursion  {mae*100:+.2f}%   (liquidation at 1x needs +71.43%)")
print(f"  exit: {exit_why} after {exit_i*0.25:.2f}h at {exit_px:.6f}")
print(f"  gross {gross:+.1f} bps  net {net:+.1f} bps  =  ${usd:+.4f}")

REAL = -5.2037
print(f"\n=== effect on the book ===")
print(f"  {'as booked (liquidation, 3x)':>34}  -1489.1 bps   ${REAL:+.4f}")
print(f"  {'counterfactual (1x, held out)':>34}  {net:+8.1f} bps   ${usd:+.4f}")
print(f"  {'difference':>34}  {'':>13} ${usd-REAL:+.4f}")
for lab, tot, n in (("live P&L as booked", 8.0590, 177),
                    ("with CASHCAT restated at 1x", 8.0590 - REAL + usd, 177),
                    ("with CASHCAT deleted entirely", 8.0590 - REAL, 176)):
    print(f"  {lab:>34}  ${tot:>+7.2f} over {n} trades  "
          f"({tot/n/NOTIONAL*1e4:+.1f} bps/trade)")
print("\nDeleting the trade assumes the position vanished. Restating it assumes the")
print("position was held at the leverage the bot now uses. Only the second is true.")
