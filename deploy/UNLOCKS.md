# Pre-unlock short alerts

`unlock_bot.py` watches token unlock schedules and tells you, through @Hyperfadebot, when
to short a token ahead of a large unlock and when to close. You place the trades by hand
on MEXC futures: MEXC's contract API has had order placement "under maintenance" since
2022, which is also why `provision_exec.py` trades spot. Stdlib only, no exchange keys.

## The trade

| | |
|---|---|
| Universe | one-day cliff unlocks ≥ 5% of circulating supply (DefiLlama schedules) on MEXC crypto perps |
| Alert | 5.5 → 1.5 days before the unlock, once per unlock, with the sizes to enter |
| Entry (you) | short $100 of the token + long $100 BTC, both Isolated 1x, Post Only at the touch |
| Exit (you) | at 00:00 UTC the day after the unlock day — the bot reminds you, and again every 6h until you mark it closed |
| Risk | no stop; the bot warns at +25% / +50% against the short; 1x isolated liquidates near +100% |

Evidence (scratchpad `unl_*.py`, 2026-10-25): 380 cliffs ≥ 5%, Mar 2024 – Oct 2026,
hedged short T-5 → end of unlock day ≈ +$3 per $100, ~66% winners, vs ~$0 for the same
short on random dates. Weaker since mid-2025 (~+$2). About 1 trade in 100 is a squeeze
that doubles against the short.

Post Only, because MEXC charges 0bp maker on these contracts but the books are 10–75bp
wide; crossing four times would cost 0.6–1.5% of a ~3% edge.

## Telegram

- alert → **✅ I shorted SYM** (starts the close reminder and squeeze watch) / **Skip**
- close reminder → **✅ Closed SYM** (records an indicative P&L from MEXC last prices)
- `/unlocks` — your open shorts, then upcoming ≥ 5% unlocks
- `/unlock_go SYM`, `/unlock_skip SYM`, `/unlock_close SYM` — the same as the buttons

Before shorting, check the date on Tokenomist or the project's own announcement.
DefiLlama is usually right; delays and shared tickers are how it is wrong (the bot already
drops a ticker whose MEXC price disagrees with CoinGecko's by > 15%). If a recorded
short's unlock disappears from the schedule, the bot warns you.

## Setup (as root, once)

```bash
mkdir -p /opt/hyperdata/unlocks && chown hyper:hyper /opt/hyperdata/unlocks
cp /opt/hyperdata/deploy/unlock-bot.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now unlock-bot
```

`telegram-monitor` already carries the `/unlocks` commands and buttons. To let `/update`
restart this unit too, add `unlock-bot` to `RESTART_UNITS` in `telegram-monitor.service`
and to the sudoers rule for the `hyper` user.

## Files (`/opt/hyperdata/unlocks`)

`candidates.json` (refreshed every 12h), `state.json` (alerts, skips, your shorts),
`requests/` (written by the monitor, consumed by the bot), `unlock_bot.log`.
