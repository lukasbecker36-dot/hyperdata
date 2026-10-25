# Pre-unlock shorts on MEXC futures

`unlock_bot.py` alerts on large token unlocks and, when you tap **Short** in Telegram,
shorts the token and longs the same notional of BTC on MEXC futures, then closes both at
the end of the unlock day. Stdlib only, like everything else here.

## The trade

| | |
|---|---|
| Universe | one-day cliff unlocks ≥ 5% of circulating supply (DefiLlama schedules) on API-tradeable MEXC crypto perps |
| Alert | 5.5 → 1.5 days before the unlock, once per unlock, with Short / Skip buttons |
| Entry | on your tap: short $100 of the token + long $100 BTC, both 1x isolated |
| Exit | 00:05 UTC on the day after the unlock day, automatically |
| Orders | post-only at the touch, re-pegged every 30s for up to 20 min per leg, then market |
| Risk | no stop; alerts at +25% / +50% against the short; 1x isolated liquidates near +100% |

Evidence (scratchpad `unl_*.py`, 2026-10-25): 380 cliffs ≥ 5%, Mar 2024 – Oct 2026,
hedged short T-5 → end of unlock day ≈ +$3 per $100, ~66% winners, vs ~$0 for the same
short on random dates. Weaker since mid-2025 (~+$2). About 1 trade in 100 is a squeeze
that doubles against the short.

Maker first because MEXC charges 0bp maker / 0–2bp taker on these contracts, but the
books are 10–75bp wide: crossing four times would cost 0.6–1.5% of a ~3% edge, and the
edge accrues over days, so waiting minutes for a passive fill is free.

## One-time setup (as root)

1. **API key**: MEXC → API management → create a key with **futures trading** only (no
   withdrawal), bound to this server's IP.
2. **Fund the futures wallet**: transfer USDT spot → futures. $200 per trade; the bot
   refuses a trade it cannot margin and says so.
3. **Secrets**:
   ```bash
   cat > /etc/hyperdata/mexc_futures.env << 'ENV'
   MEXC_FUTURES_KEY=mx0...
   MEXC_FUTURES_SECRET=...
   ENV
   chmod 600 /etc/hyperdata/mexc_futures.env && chown root:root /etc/hyperdata/mexc_futures.env
   ```
4. **Check the key (read-only, places nothing)**:
   ```bash
   set -a; . /etc/hyperdata/mexc_futures.env; set +a
   sudo -u hyper -E python3 /opt/hyperdata/mexc_futures.py      # prints balance + positions
   sudo -u hyper -E python3 /opt/hyperdata/unlock_bot.py --datadir /opt/hyperdata/unlocks --scan
   ```
5. **Service**:
   ```bash
   mkdir -p /opt/hyperdata/unlocks && chown hyper:hyper /opt/hyperdata/unlocks
   cp /opt/hyperdata/deploy/unlock-bot.service /etc/systemd/system/
   systemctl daemon-reload && systemctl enable --now unlock-bot
   systemctl restart telegram-monitor      # picks up /unlocks and the buttons
   ```
   To let `/update` restart it too, add `unlock-bot` to `RESTART_UNITS` in
   `telegram-monitor.service` and to the sudoers rule that already covers the paper bots.

Drop `--live` from the unit for a dry run: alerts and buttons work, fills are simulated at
the touch and marked PAPER.

## Telegram

- alert → **Short SYM** / **Skip**
- `/unlocks` — open trades, then upcoming ≥ 5% unlocks
- `/unlock_go SYM`, `/unlock_skip SYM` — same as the buttons, for the next unlock of SYM
- `/unlock_close SYM` — close now (maker first, then market)

Before tapping Short, check the date on Tokenomist or the project's own announcement.
DefiLlama is usually right; delays and shared tickers are how it is wrong (the bot already
drops a ticker whose MEXC price disagrees with CoinGecko's by > 15%). If an open trade's
unlock disappears from the schedule, the bot warns but does not close.

## Files (`/opt/hyperdata/unlocks`)

`candidates.json` (refreshed every 12h), `state.json` (alerts, skips, trades),
`requests/` (written by the monitor, consumed by the bot), `unlock_bot.log`.
A trade in status `check` means an open or close was interrupted: look at MEXC by hand,
then `/unlock_close SYM`.
