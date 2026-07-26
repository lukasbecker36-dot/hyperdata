# Deploying the paper bot on a Hetzner server

The bot (`paper_bot.py`) uses **only the Python 3 standard library** — no pandas/numpy, no pip installs.
You run **two independent processes** (5m and 15m) so you can compare the timeframes live.

## 1. One-time setup

```bash
# as root
apt update && apt install -y python3 git
adduser --disabled-password --gecos "" hyper
mkdir -p /opt/hyperdata && chown hyper:hyper /opt/hyperdata

# as hyper
su - hyper
git clone https://github.com/lukasbecker36-dot/hyperdata.git /opt/hyperdata
cd /opt/hyperdata
mkdir -p paper_5m paper_15m
python3 --version         # needs 3.8+
```

## 2. Quick manual test (optional, Ctrl-C after a cycle or two)

```bash
python3 paper_bot.py --interval 15m --datadir ./paper_15m
```
On start it loads the universe, calibrates the realized-vol threshold from the last 15 days
(~1–2 min), then wakes ~15s after each bar close, evaluates gates, and paper-fills via the
order book. Everything is logged to `paper_15m/`.

## 3. Run as services (survives reboots, auto-restarts)

```bash
# as root
cp /opt/hyperdata/deploy/paper-bot-5m.service      /etc/systemd/system/
cp /opt/hyperdata/deploy/paper-bot-15m.service     /etc/systemd/system/
cp /opt/hyperdata/deploy/paper-bot-15m-mid.service /etc/systemd/system/   # Phase 2 A/B arm
systemctl daemon-reload
systemctl enable --now paper-bot-5m paper-bot-15m paper-bot-15m-mid
systemctl status paper-bot-5m paper-bot-15m paper-bot-15m-mid
```

### Phase-3 Bollinger arms (15m)

Two more A/B arms test the Phase-3 finding that a Bollinger price-z-score trigger (`--trigger
bollinger`, fade `|z|>=2.5`) beat the range-breakout OOS, and whether it stacks with MID-only:

```bash
# as hyper (dirs must be owned by the service user)
mkdir -p /opt/hyperdata/paper_15m_boll /opt/hyperdata/paper_15m_boll_mid
chown -R hyper:hyper /opt/hyperdata/paper_15m_boll /opt/hyperdata/paper_15m_boll_mid
# as root
cp deploy/paper-bot-15m-boll.service     /etc/systemd/system/
cp deploy/paper-bot-15m-boll-mid.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now paper-bot-15m-boll paper-bot-15m-boll-mid
```

- `paper-bot-15m-boll` — Bollinger trigger, HIGH+MID (isolates the *trigger* change).
- `paper-bot-15m-boll-mid` — Bollinger + MID (the *combo*; best backtest Sharpe, but ~2 trades/day,
  so it validates slowly). Running both separates whether the trigger or the universe drives the edge.

Note the combo trades infrequently — expect long stretches of `0 open` before it fires.

### Avg-trade-size sizing arm (15m)

`paper-bot-15m-ats` runs the plain breakout fade (HIGH+MID, like the `15m` control) but **scales
each position's notional by the spike's avg-trade-size ratio** (`--size-by-ats`): big-trade
("whale") spikes fade harder, so they get up to 3× size; crowd spikes get as little as 0.5×. Only
the sizing differs from the `15m` control, so P&L difference isolates the whale-vs-crowd conviction
signal (see `analysis/avg_trade_size.py`). Average notional runs a bit above $100 (right-skewed).

```bash
mkdir -p /opt/hyperdata/paper_15m_ats && chown -R hyper:hyper /opt/hyperdata/paper_15m_ats
cp deploy/paper-bot-15m-ats.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now paper-bot-15m-ats
```

### Third arm: 15m MID-only (Phase 2 A/B test)

`paper-bot-15m-mid` runs the same strategy but with `--tiers MID` (drops the HIGH-liquidity tier),
writing to its own `/opt/hyperdata/paper_15m_mid/`. It is a live A/B against `paper-bot-15m`
(HIGH+MID) to confirm the Phase-2 finding that the edge concentrates in MID-liquidity names
(HIGH tier holdout Sharpe +0.6 vs MID +6.6; see `IMPROVEMENT_PLAN.md`). Same interval, only the
universe differs — so the two books are directly comparable. First `git pull` to get the
`--tiers` flag, then create the data dir **owned by the `hyper` user** (the service runs as
`User=hyper`, so a root-owned dir causes `PermissionError` on the trade-log write):

```bash
mkdir -p /opt/hyperdata/paper_15m_mid
chown -R hyper:hyper /opt/hyperdata/paper_15m_mid   # service runs as hyper, not root
```

## 3b. Trade-tape logger (forward data for VPIN / order-flow)

`tape_logger.py` (stdlib-only, minimal WebSocket client) subscribes to the `trades` channel for
the **whole active perp universe** and appends each print to `tape/tape_YYYYMMDD.csv`
(`time_ms,coin,side,px,sz,tid` — `side` = B/A aggressor, which is exactly what VPIN needs).
Historical ticks aren't available via REST, so this must run **forward** to accumulate tape.

```bash
# as hyper
mkdir -p /opt/hyperdata/tape && chown -R hyper:hyper /opt/hyperdata/tape
# as root
cp deploy/tape-logger.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tape-logger
journalctl -u tape-logger -f          # or: tail -f /opt/hyperdata/tape/tape.log
```

The log prints a per-minute `heartbeat: N trades logged` so you can confirm it's capturing.
Each finished day is **gzipped automatically** at UTC midnight (~7× smaller; the current day stays
plain `.csv` for crash-safety), so expect `tape_YYYYMMDD.csv.gz` at ~30–55 MB/day (~1–2 GB/month).
Check with `du -sh /opt/hyperdata/tape`. Old `.gz` files are kept (never auto-deleted) — add a
retention cron if you ever want to prune, but you need the history for the VPIN backtest.

## 4. Watch it

```bash
# live logs
journalctl -u paper-bot-15m -f
tail -f /opt/hyperdata/paper_15m/bot_15m.log

# trades + running P&L (last column is cumulative)
column -t -s, /opt/hyperdata/paper_15m/trades_15m.csv | less -S

# compare all arms at a glance (last row = cumulative P&L)
for d in paper_5m paper_15m paper_15m_mid; do
  echo "== $d =="; tail -1 /opt/hyperdata/$d/trades_*.csv 2>/dev/null
done

# the A/B that matters: 15m HIGH+MID (control) vs 15m MID-only
for d in paper_15m paper_15m_mid; do
  echo "== $d =="; cat /opt/hyperdata/$d/state_15m.json 2>/dev/null; echo
done
```

## 5. What it does (recap)

- **Entry gates** per closed bar: 5× volume spike + 24h range breakout + realized-vol above the
  calibrated 60th-pct threshold + breakout aligned with funding sign + HIGH/MID liquidity tier.
- **Fade** the breakout (short an up-break / long a down-break), one position per coin, ≤40 concurrent.
- **Fills (paper, maker):** best ask on a short entry / best bid on a long entry; mirror on exit.
  Assumes the resting maker order fills at the touch (optimistic — see notes).
- **Exit:** price closes back inside the prior 24h range (reclaim), or 8h backstop. No price stop.
- **P&L** logged per trade inclusive of maker fees (1.5 bps/side by default).

## Notes / knobs (top of `paper_bot.py`)

- `MAKER_FEE`, `NOTIONAL`, `MAX_POSITIONS`, `BACKSTOP_HRS`, `VOL_MULT` are constants at the top.
- **Isolated-margin leverage** (`LEVERAGE`, default 3×; `MAINT_MARGIN`, default 5%; or `--leverage`/`--maint-margin`):
  models a forced `liquidation` exit when a position's intrabar adverse move since entry crosses
  `1/LEVERAGE − MAINT_MARGIN` (e.g. 3× → ~28.3%). Set `--leverage 0` to disable. At 3× only ~0.7% of
  trades liquidate, so paper P&L is nearly unchanged; higher leverage liquidates more (and, per
  `PAPER_TRADING_ANALYSIS.md`, re-creates the stop that kills the edge). Use **isolated**, not cross.
- State (`state_*.json`) persists open positions + cumulative P&L, so a restart resumes cleanly.
- **Fill realism:** the bot assumes maker fills at the touch. This is optimistic — it does not model
  queue position or whether a real trade printed through. The next upgrade is a shadow-fill mode that
  only counts a fill when a trade actually prints through the resting price (needs the WS trade feed).
- Data is polled via REST each bar (~177 candle calls + 1 ctx call + a book call per fill). Well within
  Hyperliquid rate limits at 5m/15m cadence.

---

# Going live: `live_bot_ats.py` (REAL MONEY)

`live_bot_ats.py` is the real-money version of the `15m-ats` arm — the only arm whose
edge survived the shadow-fill audit (+$25.64 real vs +$39.70 booked over 57 trades).

**Read this first.** The audit is the reason this script exists and the reason it is
not just `paper_bot.py` with orders bolted on:

| | paper arm | live bot |
|---|---|---|
| entry fill | instant at the touch, always | post-only, rests `ENTRY_WINDOW_S` (300s), **abandons the signal if unfilled** |
| exit fill | instant at the touch, always | post-only + re-peg, then **crosses the spread** after `EXIT_GRACE_S` (600s) |
| fees | assumed 1.5 bps/side | actual `fee` from `userFills` |
| liquidation | modelled from intrabar excursion | whatever the exchange actually does; detected by reconcile |

The tape says ~76% of entries and ~75% of exits would fill in 300s. So expect **fewer
trades than the paper arm** (the misses are logged to `missed_15m_ats.csv`) and **worse
per-trade P&L than the audited $25.64** — because the audit only priced unfilled *entries*,
never the taker cost of an exit that won't fill passively. That cost is new and real.

Strategy logic (`entry_ok`, `exit_reason`, `size_mult`, `features`, `calibrate`) is
**imported from `paper_bot.Bot`**, so the live arm cannot drift from the measured arm.
Only execution is overridden.

## 1. Dependencies (first time this repo needs any)

Everything up to now was stdlib + the public `/info` endpoint. Signing orders needs the SDK:

```bash
# as root
python3 -m venv /opt/hyperdata/venv
/opt/hyperdata/venv/bin/pip install hyperliquid-python-sdk    # pulls in eth-account
chown -R hyper:hyper /opt/hyperdata/venv
```

## 2. Credentials — use an API wallet, never your main key

Generate one at <https://app.hyperliquid.xyz/API>. An API wallet can trade but **cannot
withdraw**, which is what you want sitting on a server.

```bash
mkdir -p /etc/hyperdata
cat > /etc/hyperdata/live.env << 'ENV'
HL_ACCOUNT_ADDRESS=0xYourMainAccountAddress
HL_SECRET_KEY=0xYourApiWalletPrivateKey
ENV
chmod 600 /etc/hyperdata/live.env && chown root:root /etc/hyperdata/live.env
```

`HL_ACCOUNT_ADDRESS` is the account that holds the funds; `HL_SECRET_KEY` is the API
wallet that signs for it. They are different addresses — that is expected.

## 3. Dry run first (default — no `--live`, no orders)

```bash
sudo -u hyper env HL_ACCOUNT_ADDRESS=0x... \
  /opt/hyperdata/venv/bin/python /opt/hyperdata/live_bot_ats.py --datadir /opt/hyperdata/live_15m_ats
```

Logs every order it *would* place. Note the dry run assumes the old optimistic instant
fill so the full path (place → fill → hold → exit → book) is exercisable offline — **do
not read dry-run P&L as an estimate of live P&L.** It is the number the audit disproved.

## 4. Arm it

```bash
cp /opt/hyperdata/deploy/live-bot-15m-ats.service /etc/systemd/system/
mkdir -p /opt/hyperdata/live_15m_ats && chown -R hyper:hyper /opt/hyperdata/live_15m_ats
systemctl daemon-reload && systemctl enable --now live-bot-15m-ats
journalctl -u live-bot-15m-ats -f
```

Start small. The unit ships `--notional 100 --max-gross 1000 --max-positions 10
--daily-loss-limit 50`. With ats sizing, per-trade notional ranges $50–$300 (0.5–3.0×).

## 5. Safety rails

- **Dry run is the default.** Real orders require `--live`.
- `--max-gross` — cap on total open notional. `--max-positions` — concurrency cap (10, vs
  the paper arm's 40, because real margin is finite).
- `--daily-loss-limit` — stops *opening* new positions once today's realized P&L is below
  `-X`. Does not force-close what is open.
- **Kill switch:** `touch /opt/hyperdata/live_15m_ats/KILL` → cancels all resting orders,
  crosses out of every position at market, exits. Delete the file before restarting.
- **Reconcile** runs each bar: the exchange is the source of truth. A position that vanished
  (liquidation, manual close) is dropped with a loud warning and its P&L is *not* booked —
  go read your fills. A position the bot doesn't recognise is **not** adopted, only flagged.
- Isolated leverage is set per coin (`--leverage`, default 3×) to match the paper arm.
- Sub-$10 orders are skipped (exchange minimum).

## 6. Watching it

```bash
tail -f /opt/hyperdata/live_15m_ats/bot_15m.log
column -s, -t /opt/hyperdata/live_15m_ats/trades_15m.csv | less -S
column -s, -t /opt/hyperdata/live_15m_ats/missed_15m_ats.csv | less -S   # the fill-rate truth
```

`trades_15m.csv` keeps the paper schema (so `shadow_fill2.py` and `analysis/*` still parse it)
and appends `fee_usd, entry_wait_s, exit_wait_s, exit_taker, repegs, sz`.

**The first thing to check after a few days:** the live fill rate. Count rows in
`missed_15m_ats.csv` against fills in `trades_15m.csv` — if entries fill at ~76%, the tape
audit was calibrated correctly and the real edge estimate holds. If it's much lower, the
audit was optimistic and the arm needs re-judging before any size increase. Also watch
`exit_taker` — every `1` is an exit that paid the spread, a cost no backtest or paper arm
ever charged.
