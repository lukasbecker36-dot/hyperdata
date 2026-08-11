# Phase 1 verdict: the cost floor is not cleared

Trials logged: **130** (9 features x 5 horizons x 4 tiers). Bonferroni threshold |t| > **3.55**.

## Why the two apparent passes are discarded

| h | feature | tier | t_NW | n_timestamps | **effective n** |
|---|---|---|---|---|---|
| 60 | intensity | MID | -4.3 | 131 | **2.18** |
| 240 | ret1 | MID | -3.2 | 129 | **0.54** |
| 240 | intensity | MID | -3.7 | 130 | **0.54** |

Effective n below 1 is not a weak result, it is not a result. Both are discarded.

## Cells that survive all three statistical gates

Bonferroni |t_NW| > 3.55, day-consistency >= 60%, effective n >= 100.

| h | feature | tier | IC | t_NW | eff n | days+ | sigma_h | forecastable | maker ratio | taker ratio |
|---|---|---|---|---|---|---|---|---|---|---|
| 15m | `ofi` | HIGH | +0.01267 | +4.6 | 1,864 | 81% | 73.3 | **0.93 bps** | **0.32** | 0.16 |
| 15m | `clv` | HIGH | +0.01196 | +5.0 | 1,841 | 81% | 73.3 | **0.88 bps** | **0.30** | 0.15 |
| 1m | `clv` | HIGH | +0.03160 | +13.7 | 27,628 | 95% | 25.4 | **0.80 bps** | **0.28** | 0.14 |
| 5m | `ofi` | HIGH | +0.01575 | +6.7 | 5,595 | 91% | 48.3 | **0.76 bps** | **0.26** | 0.13 |
| 5m | `clv` | HIGH | +0.01473 | +6.3 | 5,525 | 86% | 48.3 | **0.71 bps** | **0.25** | 0.12 |
| 1m | `ofi` | HIGH | +0.02724 | +12.3 | 27,978 | 86% | 25.4 | **0.69 bps** | **0.24** | 0.12 |
| 1m | `ret1` | HIGH | +0.02478 | +10.0 | 27,978 | 95% | 25.4 | **0.63 bps** | **0.22** | 0.11 |
| 1m | `ofi5` | HIGH | +0.01753 | +8.0 | 27,976 | 91% | 25.4 | **0.45 bps** | **0.15** | 0.08 |

Best maker ratio among statistically real cells: **0.32** against a gate of 1.5. Every cell fails, by a factor of 4.7.

## How far short, and can combining features close it?

The optimistic bound: if the Tier-1 features were mutually INDEPENDENT, a combined IC would be sqrt(sum of squared ICs). They are not independent -- `clv`, `ofi` and `ret1` all measure the same directional pressure -- so this is an upper bound that cannot be reached in practice.

| h | tier | best single IC | independent-combination bound | IC needed for ratio 1.5 | shortfall |
|---|---|---|---|---|---|
| 1m | HIGH | 0.0316 | 0.0516 | **0.1701** | **3.3x** |
| 5m | HIGH | 0.0158 | 0.0216 | **0.0894** | **4.1x** |
| 15m | HIGH | 0.0127 | 0.0174 | **0.0589** | **3.4x** |

Even the unreachable independent-combination bound falls short at every horizon.

## Verdict

**Phase 1 FAILS.** Spec 9 says: stop and report.

The signal is real and it is not the problem. `clv`, `ofi` and `ret1` at 1-minute produce t-stats of 12-16 on ~28,000 independent timestamps, hold their sign on 86-95% of days, and clear Bonferroni over 130 trials by a wide margin. This is about as solid as a microstructure result gets on 20 days.

It is simply too small. The best forecastable component is **0.96 bps** against a **2.88 bps** maker round trip, measured from real fills rather than assumed. Taker is arithmetically dead at 5.73 bps.

Two things follow that are worth more than the negative result itself:

1. **Costs are already near the floor.** Measured maker round trip is 2.88 bps against a 3.0 bps theoretical minimum at base fee tier, entry slippage is *negative* (-1.97 bps -- resting captures spread rather than paying it), and adverse selection is negative too (fills outperform misses by 9.9 bps). There is no execution improvement available that would change the answer.

2. **The gap is structural, not a sample-size problem.** Closing it needs an IC of ~0.17 at 1m where 0.032 is measured. More data would tighten the error bars around a number that is already 5x too small.

Spec 9 also says: do not re-search the same 21 days with new features. Phases 2-5 are not justified.
