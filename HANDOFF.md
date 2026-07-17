# Task brief: Hyperliquid volume-spike vs price analysis

## Goal
Pull historical OHLCV **candle** data for a Hyperliquid perp (starting with the S&P 500 perp,
ticker `SPX`), then flag unusual volume spikes and measure the associated price moves.
Output a CSV ready for charting.

## Environment note
Run this on the local machine (network egress works here). Use Python with `requests`
(REST) and optionally `websockets` (live tape). No API key or auth is needed for public
market data — the info endpoint is open.

## Confirmed facts (already verified against the live API)
- The S&P 500 perpetual's API ticker is **`SPX`** (confirmed via the `meta` endpoint).
- It launched on Hyperliquid on 2026-03-18, so no candle data exists before that date.

## What the API can and cannot give you
- **Raw trade-by-trade tape historically: NOT available from the public REST API.**
  `recentTrades` returns only a small recent snapshot (no time range). Live trades come from
  the WebSocket `trades` channel (forward-only). True historical raw trades live only in the
  requester-pays S3 archive `s3://hl-mainnet-node-data/` (needs an AWS account; all-coins, huge).
- **Historical OHLCV + volume: fully available via `candleSnapshot`.** This is the right tool
  for volume-spike-vs-price analysis. Use it.

## Endpoints (all POST to `https://api.hyperliquid.xyz/info`, `Content-Type: application/json`)

### List perps (find/confirm tickers)
```json
{ "type": "meta" }
```
Returns `{ "universe": [ { "name": "SPX", "szDecimals": ..., ... }, ... ] }`.

### Candles (main data source)
```json
{ "type": "candleSnapshot",
  "req": { "coin": "SPX", "interval": "1h",
           "startTime": 1710720000000, "endTime": 1713312000000 } }
```
- `startTime`/`endTime` are **Unix milliseconds**.
- Response is a list of candle objects with these fields:
  - `t` open time (ms), `T` close time (ms), `s` symbol, `i` interval
  - `o` open, `h` high, `l` low, `c` close, `v` volume, `n` number of trades
  - **`o/h/l/c/v` come back as strings** — cast to float. `n` is an int.
  - `v` is volume in **base units** (contracts/coin), not USD. For USD volume, multiply by a
    representative price for the candle (e.g. close, or (h+l+c)/3).
- **Limit: ~5000 candles per request.** Page through with a rolling window for fine intervals.
- Valid intervals: `1m,3m,5m,15m,30m,1h,2h,4h,8h,12h,1d,3d,1w,1M`.

### Recent trades (only if you specifically need the tape; snapshot only)
```json
{ "type": "recentTrades", "coin": "SPX" }
```

### Live trades (forward-only, optional)
`wss://api.hyperliquid.xyz/ws`, then send:
```json
{ "method": "subscribe", "subscription": { "type": "trades", "coin": "SPX" } }
```

## Rate limits / etiquette
- The info endpoint is IP weight-limited (roughly ~1200 weight/min). When paginating, add a
  small `time.sleep(0.1–0.2)` between requests and handle HTTP 429 with backoff.

## Suggested implementation
1. Params: `coin` (default `SPX`), `interval`, `start`, `end`.
2. Paginate `candleSnapshot`: chunk the window so each request stays under ~5000 candles
   (e.g. for `1m`, step ~5000 minutes per call), stitch results, dedupe on `t`, sort by `t`.
3. Build a DataFrame; cast numeric fields to float.
4. Feature engineering for "unusual volume":
   - rolling median/mean & std of `v` over a trailing window (e.g. 20–50 candles)
   - `vol_z = (v - rolling_mean) / rolling_std`  and/or  `vol_ratio = v / rolling_median`
   - flag spikes where `vol_z >= 3` (or `vol_ratio >= 3`)
5. Price reaction columns:
   - `ret_same = c/o - 1` (same-candle directional move)
   - `range = (h - l) / o` (same-candle volatility)
   - `ret_next = next_close/close - 1` (lagged reaction)
   - optionally `n` and `avg_trade_size = v / n` to distinguish whale vs crowd spikes
6. Write everything to `spx_candles_<interval>.csv`. Print a short summary of flagged spikes.

## Caveat to keep in mind
Candles aggregate within the interval, so within-bar ordering is lost — good for "spikes
coincide with big moves," but it can't cleanly prove volume *leads* price. Use fine intervals
(1m) to tighten this; only go to the S3 raw tape if you need true tick-level lead/lag.
