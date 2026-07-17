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

## 5. Telegram monitoring (optional but recommended)

Zero extra dependencies — `telegram_notify.py` talks to the Telegram Bot API over the
stdlib `urllib`. You get **push alerts** (start / each OPEN & CLOSE / daily summary / errors)
from the trading bots, and an **interactive monitor** you can query with `/status`, `/pnl`,
`/positions`, `/trades`.

### 5.1 Create the bot + get your chat id (one time, ~2 min)

1. In Telegram, message **@BotFather** → `/newbot`, pick a name and username.
   It replies with a **token** like `123456789:ABCdef...`. Keep it secret.
2. **Send your new bot any message** (e.g. `hi`) from the account/group that should
   receive alerts. This is required before Telegram will reveal the chat id.
3. Find your chat id:
   ```bash
   TELEGRAM_BOT_TOKEN=123456789:ABCdef... python3 /opt/hyperdata/telegram_notify.py
   # prints e.g.  chat_id=987654321  (yourname)
   ```

### 5.2 Store the credentials on the server (not in git)

```bash
# as root
mkdir -p /etc/hyperdata
cat > /etc/hyperdata/telegram.env <<'EOF'
TELEGRAM_BOT_TOKEN=123456789:ABCdef...
TELEGRAM_CHAT_ID=987654321
EOF
chown -R hyper:hyper /etc/hyperdata
chmod 600 /etc/hyperdata/telegram.env      # secrets: keep it locked down
```

The two bot units read this file via `EnvironmentFile=-...` (the `-` makes it optional, so the
bots still run if the file is absent — they just won't push). The monitor requires it.

### 5.3 Turn it on

```bash
# push alerts: just restart the (already-updated) bot units so they pick up the env file
systemctl daemon-reload
systemctl restart paper-bot-5m paper-bot-15m

# interactive monitor: install + start its service
cp /opt/hyperdata/deploy/telegram-monitor.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now telegram-monitor
systemctl status telegram-monitor
```

You should get a "🤖 paper bot started" push from each bot and a "📱 monitor online" push.
Now send `/help` to your bot in Telegram.

### 5.4 Notes

- **One token, one poller.** Only the monitor calls `getUpdates`; the bots only *send*. Don't run a
  second `getUpdates` consumer against the same token (Telegram returns 409 conflict).
- **Alert volume:** you get a message per OPEN and per CLOSE. Signals are rare/conditioned, but if it's
  ever noisy, the per-trade pushes live in `open_pos`/`close_pos` in `paper_bot.py` — comment out the
  `self.notify(...)` calls to keep only startup + daily summary + errors.
- **Access control:** the monitor answers only messages from `TELEGRAM_CHAT_ID`; everyone else is ignored.
- **Multiple recipients:** point `TELEGRAM_CHAT_ID` at a Telegram *group* chat id (add the bot to the
  group first) to alert several people at once.

## 6. What it does (recap)

- **Entry gates** per closed bar: 5× volume spike + 24h range breakout + realized-vol above the
  calibrated 60th-pct threshold + breakout aligned with funding sign + HIGH/MID liquidity tier.
- **Fade** the breakout (short an up-break / long a down-break), one position per coin, ≤40 concurrent.
- **Fills (paper, maker):** best ask on a short entry / best bid on a long entry; mirror on exit.
  Assumes the resting maker order fills at the touch (optimistic — see notes).
- **Exit:** price closes back inside the prior 24h range (reclaim), or 8h backstop. No price stop.
- **P&L** logged per trade inclusive of maker fees (1.5 bps/side by default).

## Notes / knobs (top of `paper_bot.py`)

- `MAKER_FEE`, `NOTIONAL`, `MAX_POSITIONS`, `BACKSTOP_HRS`, `VOL_MULT` are constants at the top.
- State (`state_*.json`) persists open positions + cumulative P&L, so a restart resumes cleanly.
- **Fill realism:** the bot assumes maker fills at the touch. This is optimistic — it does not model
  queue position or whether a real trade printed through. The next upgrade is a shadow-fill mode that
  only counts a fill when a trade actually prints through the resting price (needs the WS trade feed).
- Data is polled via REST each bar (~177 candle calls + 1 ctx call + a book call per fill). Well within
  Hyperliquid rate limits at 5m/15m cadence.
