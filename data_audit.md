# Phase 0 data audit

Generated 2026-08-11 00:09 UTC

## Candle files

| file | bar | rows | coins | from | to | span (d) | NaN cells | gaps |
|---|---|---|---|---|---|---|---|---|
| `hyperliquid_1m_48h.csv` | 1m | 28,806 | 10 | 2026-07-15 13:36 | 2026-07-17 13:36 | 2.0 | 0 | 0 (0.0%) |
| `hyperliquid_1m_60d.csv` | 1m | 51,136 | 10 | 2026-07-13 23:03 | 2026-07-17 13:45 | 3.6 | 0 | 0 (0.0%) |
| `hyperliquid_5m.csv` | 5m | 885,677 | 177 | 2026-06-30 10:15 | 2026-07-17 22:45 | 17.5 | 0 | 0 (0.0%) |
| `hyperliquid_15m_60d.csv` | 15m | 50,082 | 10 | 2026-05-26 08:30 | 2026-07-17 13:45 | 52.2 | 0 | 0 (0.0%) |
| `hyperliquid_15m_allperps.csv` | 15m | 878,387 | 177 | 2026-05-26 08:15 | 2026-07-17 13:45 | 52.2 | 0 | 0 (0.0%) |
| `hyperliquid_1h_history.csv` | 1h | 871,117 | 177 | 2025-12-19 15:00 | 2026-07-17 15:00 | 210.0 | 0 | 0 (0.0%) |

## Raw tape

2 files found

## `tape_buckets.csv.gz` (derived, 5-min)

- rows **1,001,581**, coins **177**
- span **2026-07-21 16:00 to 2026-08-10 21:35**  (20.2 days)

### Coins active per day

| day | coins | | day | coins |
|---|---|---|---|---|
| 2026-07-21 | 177 | | 2026-08-01 | 177 |
| 2026-07-22 | 177 | | 2026-08-02 | 177 |
| 2026-07-23 | 177 | | 2026-08-03 | 177 |
| 2026-07-24 | 177 | | 2026-08-04 | 177 |
| 2026-07-25 | 177 | | 2026-08-05 | 177 |
| 2026-07-26 | 177 | | 2026-08-06 | 177 |
| 2026-07-27 | 177 | | 2026-08-07 | 177 |
| 2026-07-28 | 177 | | 2026-08-08 | 177 |
| 2026-07-29 | 177 | | 2026-08-09 | 177 |
| 2026-07-30 | 177 | | 2026-08-10 | 177 |
| 2026-07-31 | 177 | |  |  |

## Overlap: tape vs each candle file

Spec 11 forbids joining tape flow to the candle files. This is why:

| candle file | candle ends | tape starts | overlap |
|---|---|---|---|
| `hyperliquid_1m_48h.csv` | 2026-07-17 13:36 | 2026-07-21 16:00 | **NONE — gap of 4.1 days** |
| `hyperliquid_1m_60d.csv` | 2026-07-17 13:45 | 2026-07-21 16:00 | **NONE — gap of 4.1 days** |
| `hyperliquid_5m.csv` | 2026-07-17 22:45 | 2026-07-21 16:00 | **NONE — gap of 3.7 days** |
| `hyperliquid_15m_60d.csv` | 2026-07-17 13:45 | 2026-07-21 16:00 | **NONE — gap of 4.1 days** |
| `hyperliquid_15m_allperps.csv` | 2026-07-17 13:45 | 2026-07-21 16:00 | **NONE — gap of 4.1 days** |
| `hyperliquid_1h_history.csv` | 2026-07-17 15:00 | 2026-07-21 16:00 | **NONE — gap of 4.0 days** |

The tape begins **2026-07-21 16:00**. Every candle file ends on or before **2026-07-17 22:45**. There is no bar in common, so all flow features must be built on tape-derived bars.

