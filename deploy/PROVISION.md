# Deploying the MEXC dip-provision paper bot

A second, independent strategy alongside the Hyperliquid fade bots. Same conventions:
**Python 3 standard library only**, no pip installs, systemd-managed, Telegram alerts via
the existing `telegram_notify.py` and `/etc/hyperdata/telegram.env`.

Different exchange, though. The Hyperliquid bots trade `api.hyperliquid.xyz`; this one reads
`contract.mexc.com` public endpoints. **Paper only — it places no orders and needs no API key.**

## Why MEXC and not Hyperliquid

The strategy trades perps in their first 45 days after listing. Checked on 2026-07-25:

| venue | perps inside Day 3-45 right now |
|---|---|
| Hyperliquid | **2** (CASHCAT, GRAM) |
| MEXC | **26** crypto (164 including tokenised equities, which are filtered out) |

Hyperliquid simply does not list enough new perps to test this — two names would produce a
handful of trades a month. MEXC lists ~14/month and is the venue the edge was measured on.
The Hetzner box, systemd pattern and Telegram plumbing carry over unchanged; only the market
data source differs.

## The strategy

| | |
|---|---|
| Universe | crypto USDT perps, listing age Day 3-45 (equities/indices/commodities filtered) |
| Entry | resting BUY 5% below the prior hourly close, long only |
| Exit | resting SELL at +10% (maker), or market exit after 72h (taker). **No stop.** |
| Size | $100/lot, max 5 lots per symbol -> cash bounded at $500/symbol |
| Gates | bar must trade >=20x the lot size; price must trade 0.2% *through* the level |

No leverage and no liquidation path, by construction. That is the point — the squeeze tail
that made every short configuration in this study break even cannot reach a cash-bounded long.

Backtest (219 tokens, 18 months): +$31/token per 42-day window, positive median, ~46% return
on average deployed capital, positive in all six launch quarters, and it beat random entry at
identical exposure by +$35/token with a 95% CI of [+18, +53]. The same rule at a 2% distance
was indistinguishable from long beta, so the 5% distance is load-bearing, not tuned.

## What this run is actually for

The backtest's one weakness cannot be fixed with candles: **hourly OHLC cannot tell you whether
a resting bid 5% below the market would have filled.** A bar low proves a trade printed at that
price. It does not prove our order was near the front of the queue.

So every order the bot places records both verdicts:

- **`bar_fill`** — the backtest's rule: the low pierced the level and the volume gate passed.
  *Optimistic.*
- **`queue_fill`** — sell-aggressor volume printing at or below our level exceeded the USD
  resting at or above it when we placed, plus our own size. *Conservative* — it charges us for
  the entire visible book being **traded** through, when much of it is really cancelled.

The true fill rate is between the two. First live snapshot: median queue ahead of a $100 bid at
-5% was **$8,680**, against $35k median hourly volume. That is the number that decides whether
this is real, and it is why the run has to happen forward.

P&L is booked on `bar_fill`, so the live run stays directly comparable to the backtest, while
`orders.csv` accumulates the fill evidence separately.

## Fees, and a wrinkle the backtest could not see

MEXC prices brand-new listings differently from mature ones, and gates the API:

| cohort | maker | taker | apiAllowed |
|---|---|---|---|
| currently new (Day 3-45) | 4bp | 10bp | **False for 16 of 28** |
| the same tokens, later | **0bp** | 2bp | True for 217 of 219 |

`apiAllowed` and the fee schedule are read from *current* contract detail, so the backtest
priced the study's tokens at the fees they carry **today** (zero maker), not the ones in force
during their Day 3-45 window. Re-running with real per-symbol fees barely moved the result
(+$35.2 -> +$32.4 per token), but the universe restriction is not something history can settle.
Hence two arms:

- **`provision-bot`** — all Day 3-45 crypto listings. More data, faster validation.
- **`provision-bot-api`** — `--api-only`, the subset actually tradeable via API today. Slower,
  but it is the only arm describing a strategy you could switch to live.

Run both; the difference between them *is* the answer.

## 1. Install

```bash
# as hyper
cd /opt/hyperdata && git pull
mkdir -p paper_provision paper_provision_api
```

Files needed at the repo root: `provision_bot.py`, `mexc_api.py`, `provision_report.py`.

## 2. Smoke-test before enabling anything

```bash
python3 provision_bot.py --datadir ./paper_provision --once
```

One cycle takes ~35s: it loads the universe, snapshots the book for each symbol and places
resting bids for the bar now starting. Expect output like:

```
universe: 26 symbols in Day 3-45 (11 apiAllowed, 15 not)
placed 26 resting bids for bar 2026-07-25 21:00:00
cycle done: 26 resting, 0 lots, cum=$+0.00, closed=0, orders=0
```

Nothing fills on the first cycle by design — orders are placed for the *next* bar and resolved
an hour later. `orders.csv` gets its first rows after the second cycle.

## 3. Run as services

```bash
# as root
cp /opt/hyperdata/deploy/provision-bot.service     /etc/systemd/system/
cp /opt/hyperdata/deploy/provision-bot-api.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now provision-bot provision-bot-api
systemctl status provision-bot provision-bot-api
```

The data dirs must be owned by the service user (`User=hyper`), or the CSV write fails with
`PermissionError` — the same trap as the 15m-mid arm:

```bash
chown -R hyper:hyper /opt/hyperdata/paper_provision /opt/hyperdata/paper_provision_api
```

## 4. Watch it

```bash
journalctl -u provision-bot -f
tail -f /opt/hyperdata/paper_provision/bot.log

# the report — fill-rate bracket, P&L, and run rate vs the backtest
python3 provision_report.py /opt/hyperdata/paper_provision
python3 provision_report.py /opt/hyperdata/paper_provision_api
```

Telegram pushes a message on every fill and every close, plus a daily summary carrying the two
fill rates.

## 5. Reading it — what would confirm or kill the strategy

Expect a slow burn. ~26 symbols x ~0.5 fills/symbol/day is roughly **10-13 fills/day**, and a
losing lot sits for the full 72h while winners leave early, so **early P&L is biased DOWN**.
Give it 3-4 weeks before drawing conclusions.

**Kills it:**
- `queue_fill` far below `bar_fill` (say under 30% of assumed fills) *and* P&L on
  queue-confirmed fills near zero — the backtest was filling orders that would never have filled.
- Queue-confirmed fills systematically worse than the rest: adverse selection, i.e. we only
  really fill when we are about to be run over.
- Target-exit share far below the backtest's ~56%.

**Confirms it:**
- Fills per token per 42d near 23, target-exit share near 56%, P&L per token per 42d near +$31.
- The `api-only` arm tracking the full arm — the tradeable subset is not special.

## Notes / knobs

- Strategy constants sit at the top of `provision_bot.py` (`DIP`, `TP`, `MAX_HOLD_H`,
  `MAX_LOTS`, `LOT_USD`, `ENTRY_BUFFER`, `VOL_GATE`). **Do not tune them against the live run** —
  the whole point is an out-of-sample test of one pre-registered configuration.
- `--lot` overrides the lot size. The backtest held its return on capital to ~$2,000 lots and
  decayed by $10,000, so `--lot 500` is a reasonable second size arm later. Not before the
  $100 arm has answered the fill question.
- MEXC trades whole contracts, so a lot is rounded to an integer contract count; symbols where
  one contract costs more than 2x the lot budget are skipped (`min_contract_size` in
  `orders.csv`).
- Funding is fetched per close and applied over the actual hold. Negative funding means the long
  was *paid* — the study found funding mildly favours longs on these names.
- State (`state.json`) persists resting orders, open lots and cumulative P&L, written atomically,
  so a restart resumes cleanly. Orders left unresolved by downtime are dropped rather than
  guessed at (`drop stale order` in the log).
- Request load: ~1 detail + ~26 klines + ~26 depth per hour, plus one `/deals` poll per resting
  order every 20s (~1.3 req/s). MEXC's public contract limit is ~20 req/2s, so there is ample
  headroom.
