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

## 3. Run both as services (survives reboots, auto-restarts)

```bash
# as root
cp /opt/hyperdata/deploy/paper-bot-5m.service  /etc/systemd/system/
cp /opt/hyperdata/deploy/paper-bot-15m.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now paper-bot-5m paper-bot-15m
systemctl status paper-bot-5m paper-bot-15m
```

## 4. Watch it

```bash
# live logs
journalctl -u paper-bot-15m -f
tail -f /opt/hyperdata/paper_15m/bot_15m.log

# trades + running P&L (last column is cumulative)
column -t -s, /opt/hyperdata/paper_15m/trades_15m.csv | less -S

# compare the two timeframes at a glance
for tf in 5m 15m; do
  echo "== $tf =="; tail -1 /opt/hyperdata/paper_$tf/trades_$tf.csv
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
