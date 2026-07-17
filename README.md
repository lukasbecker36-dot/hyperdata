# hyperdata — Hyperliquid perps: volume-breakout fade research

Research into whether volume spikes predict price moves on Hyperliquid perpetuals, and
whether a tradeable mean-reversion strategy can be built around them.

## Headline finding
Raw volume spikes predict move **size**, not direction. But a **volume spike (≥5× trailing-24h
median) at a range breakout** systematically **reverts** — fade the breakout. The raw signal is
non-stationary (only worked Jun–Jul 2026), but conditioning on **high realized volatility +
a breakout aligned with crowded funding**, held ~8h with **no stop-loss**, recovers a persistent
edge across 8 months (~+25bps net/trade, 56% win, in-sample Sharpe ~3.8). Stop-losses *destroy*
the edge — the negative skew is the risk premium.

Interactive report: see `backtest_report.html`.

## Data (all pulled from `https://api.hyperliquid.xyz/info`)
| File | Contents |
|---|---|
| `hyperliquid_1m_48h.csv` | 1m candles, 48h, the original 10 names |
| `hyperliquid_15m_60d.csv` | 15m candles, ~52d, original 10 names |
| `hyperliquid_15m_allperps.csv` | 15m candles, ~52d, all 175 active core perps |
| `hyperliquid_1h_history.csv` | 1h candles, ~208d (Dec 2025→Jul 2026), all perps |
| `hyperliquid_funding.csv` | hourly funding rates, ~208d, all perps |
| `perp_universe.csv` | active perp names + 24h notional volume |

## Pipeline (roughly in order)
- `fetch_*.py` — data pulls (candles, funding). Note API retains only ~5000 candles/interval.
- `vol_price_study.py` — initial volume vs price move study (1m).
- `study_context.py` / `study_all.py` — context-conditioned spike study (breakout vs in-range).
- `backtest.py` — event-driven backtest, cost sensitivity, walk-forward.
- `hist_study.py` — 8-month monthly decay study (1h).
- `maker_exec.py` — maker execution / adverse-selection model.
- `regime_vol_mom.py`, `regime_vol_deep.py` — volatility + momentum/EMA regime filters.
- `funding_study.py`, `validate_stack.py` — funding regime filter + stacked-filter validation.
- `stop_target.py`, `sizing.py` — stop/target sweep + volatility-scaled sizing.

## Caveats
In-sample only; signals are time-clustered/correlated; edge concentrates in MID-liquidity names;
regime-filter thresholds use full-sample quantiles (mild lookahead). Not investment advice.

## Live paper bot
See `deploy/DEPLOY.md`. Run `paper_bot.py --interval 5m` and `--interval 15m` as two processes (systemd units in `deploy/`). Stdlib-only, self-calibrating, logs P&L inclusive of maker fees.

**Telegram monitoring** (optional, still stdlib-only): set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` and the bots push start/OPEN/CLOSE/daily-summary/error alerts. `telegram_monitor.py` (its own systemd unit) answers `/status`, `/pnl`, `/positions`, `/trades`. Setup walkthrough in `deploy/DEPLOY.md` §5.
