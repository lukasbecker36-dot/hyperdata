# Paper-trading P&L analysis + risk/sizing/leverage study

**Window analysed:** live paper bot, 2026-07-18 → 2026-07-20 (2 days, 5m + 15m books).
**Backtest reference:** 2,604 stacked signals over ~8 months (Dec 2025 → Jul 2026), 1h candles.
**Bottom line:** the live edge is intact; the losses came from the strategy's designed
negative-skew tail. No structural filter (stop, trend gate, concurrency cap, vol-scaling)
improves risk-adjusted return — the tail and the edge are the *same* trades. The only
lever that works is **flat notional sizing**, and leverage must stay **≤ ~3×** or
liquidation re-creates the lethal stop.

Reproduce everything with the stdlib scripts in [`analysis/`](analysis/) (no pandas needed).

---

## 1. Live paper P&L (Jul 18–20)

| | 5m book | 15m book |
|---|---|---|
| Closed trades | 86 | 56 |
| Win rate | 86.0% | 87.5% |
| **Cumulative P&L** | **−$38.97** | **−$0.09** |
| Reclaim exits | 73 → **+$52.9** | 48 → **+$56.7** |
| Backstop (8h) exits | 13 → **−$91.9** | 8 → **−$56.8** |

*(P&L at $100 notional/trade, inclusive of 3 bps round-trip maker fee.)*

The high win rate with a negative total is the strategy's signature: many small reclaim
winners, occasionally overwhelmed by a few fade-gets-run-over losers that ride the 8h
backstop with no stop. **On both books the reclaim exits are solidly positive** (+$53, +$57);
every dollar of loss comes from the backstop tail.

**Two trades drive each book's entire drawdown:**

| Book | Worst 2 backstops | Their P&L | Book *excluding* those 2 |
|---|---|---|---|
| 5m | HEMI −$25.3, ACE −$23.5 | −$48.8 | **+$9.8** |
| 15m | HEMI −$17.7, PUMP −$15.5 | −$33.2 | **+$33.1** |

- **ACE** (5m): shorted an up-breakout at 0.0742, ran to 0.0916 (**+23%** against) → −2,348 bps.
- **HEMI** (both books): shorted ~0.00476, ran to ~0.00596 (**+25%**) → −2,528 bps (5m), −1,769 bps (15m).

**It was one correlated regime event, not 86 independent draws.** Nearly all big losers were
up-breakout SHORT fades steamrolled in the Jul 19–20 rally — ACE, HEMI, PUMP, MET, KAITO,
CASHCAT all backstopped in the same ~24h window, *on both books simultaneously*. That proves
it's the signal/regime (the README's "signals are time-clustered/correlated" caveat), not an
execution artifact.

---

## 2. Three risk fixes tested — and rejected

### 2a. Same-direction concurrency cap — **not robust**
Replayed on the actual live trades (real fills, real P&L). Cap the number of concurrent
same-side positions; skip entries above the cap.

| | 5m (base −$38.97) | 15m (base −$0.09) |
|---|---|---|
| SHORT cap = 2 | −$27.35 (**+$11.6**) | −$15.96 (**−$15.9 worse**) |

The benefit **flips sign between two timeframes of the same 2 days**. It doesn't clip risk;
it just removes trades, and whether that helps depends on whether the clustered names happened
to lose. On 15m the clustered shorts were the KAITO *winners*. It also misses the biggest loser
(HEMI wasn't in a dense cluster).

### 2b. Trend gate — **degenerate**
Split signals into "fade a momentum breakout" (aligned with trailing trend) vs "fade an
exhaustion breakout" (counter-trend). Result: **COUNTER-trend breakouts = 0** at every lookback.
A breakout *is* a move to a new extreme, so it's always locally trend-aligned; the crowd-funding
filter reinforces it. "Don't fade a trend-aligned breakout" would gate 100% of trades.

Reduced to a magnitude gate (skip biggest preceding moves), it truncates the worst single trade
(−42.6% → ~−15%) but the dropped trades are net **positive** (+11 to +26 bps) — it buys lower
variance with proportionally lower return, not free tail removal.

### 2c. Catastrophic-wide stop — **fails at every width**
8-month sweep. The repo previously only tested stops down to 3%; live blowups were ~25%, so
we swept the 5–12% band too.

| Stop | net bps/trade | worst trade | 8-mo cum |
|---|---|---|---|
| **none (hold)** | **+24.9** | −37.6% | **+647%** |
| 3% | −15.1 | −3.1% | −393% |
| 6% | −20.4 | −6.1% | −532% |
| 10% | −15.2 | −10.1% | −397% |
| 12% | −7.1 | −12.4% | −186% |

**Every stop turns +647% into a loss.** Why: a 6% stop fires on 612 trades; held to 8h those
average **−4.2%**, but the stop locks them at **−6.1%**. Only 216 were genuinely catastrophic —
the other 396 dipped 6% intrabar and then **reverted**. The strategy enters at an overshoot and
profits from the snap-back; intrabar, price routinely pushes *further* into the overshoot first,
so any stop sells at the deepest point — right before the reversion it's designed to capture.
**The stop systematically sells the bottom.**

> A 6% stop *would* have capped live ACE (−23% → −6%) and HEMI (−25% → −6%), saving ~$36. That's
> the trap: judged on the two visible disasters a stop looks obvious, but over 8 months the same
> stop bleeds you on the 396 invisible reverting trades. The live window was a tail-heavy sample;
> the backtest is the corrective one.

---

## 3. Sizing — the only lever that works

Concurrency-aware, dollar-based, 8-month backtest. Measured by **return per unit of drawdown**:

| Sizing / lever | 8-mo return | maxDD | worst 48h cluster | **return / \|DD\|** |
|---|---|---|---|---|
| **Flat $100, uncapped** | +$647 | −$308 | −$253 | **2.10** ← best |
| Flat $100, cap 40 (bot cfg) | +$381 | −$314 | −$263 | 1.21 |
| Flat $100, cap 20 | +$274 | −$283 | −$208 | 0.97 |
| Flat $100, cap 10 | +$219 | −$250 | −$190 | 0.88 |
| Vol-scaled ~$100 | +$328 | −$368 | −$263 | 0.89 |

- **Vol-scaling backfires**: it shrinks the worst *single* trade ($38 → $26) but *increases*
  maxDD ($308 → $368) and halves return, because it down-weights the high-vol names that carry
  the edge. (An earlier daily-normalized Sharpe view hid this; the dollar-cluster sim exposes it.)
- **Concurrency caps barely touch drawdown**: the DD is a multi-day losing *regime*, not an
  instantaneous pile-up. Capping cuts peak deployed capital hard (82→40→10 positions =
  $8,200→$4,000→$1,000) but maxDD only drifts $308→$250 while return collapses.
- **Only flat notional preserves return/|DD| = 2.10** — it scales return and drawdown by the same
  factor. You don't optimize the tail away; you *size* for it.

### Sizing to a risk budget

Per **$100 notional** (bot's cap-40 config): worst single ≈ **−$38**, worst 48h cluster ≈ **−$263**,
worst peak-to-trough ≈ **−$314**. The live −$67 drawdown was only ~¼ of the historical worst
cluster — **you have not yet seen the real tail.**

| Max tolerable drawdown | Set flat notional to |
|---|---|
| $150 | ~$48 |
| $300 | ~$95 (≈ current $100) |
| $500 | ~$160 |
| $1,000 | ~$320 |

*notional = $100 × your_limit ÷ $314.* These are realized-at-close; peak mark-to-market is deeper
— pad ~1.3–1.5×, i.e. **treat $100 notional as a ~−$400 MtM worst-case budget.**

---

## 4. Leverage / capital efficiency

Because the strategy holds through overshoots with **no stop, leverage is a stop** — liquidation
is a forced exit at the overshoot bottom. So the binding limit is the position's **maximum
adverse excursion (MAE)**: the worst intratrade move before the reversion.

**MAE distribution (8-month, fraction moved *against* the position):**

| median | p90 | p95 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| 2.8% | 10.2% | 13.2% | 22.5% | 58.4% | 129.3% |

And these big excursions **still revert**: of trades with MAE ≥ 10%, 14% still closed positive.
Liquidating them realises a max loss *and* forfeits the reversion.

**Liquidation rate at a fixed isolated leverage (maintenance margin ≈ 5%):**

| Leverage | Liquidates at adverse move | % of trades liquidated | of those, % that would've reverted to a win |
|---|---|---|---|
| 2× | 45% | 0.2% | 17% |
| **3×** | **28%** | **0.7%** | 33% |
| 5× | 15% | 3.8% | 13% |
| 10× | 5% | 29.9% | 22% |
| 20× | 0% | 100% | 56% |

**Recommendation: cap leverage at ~3×.** It liquidates only 0.7% of trades, needs just **~$33
margin per $100 notional** (≈3× more capital-efficient than posting full notional), and stays
below the point where liquidation starts eating reverting winners. Above 5× the strategy
self-destructs — 10× liquidates ~30% of trades, re-creating the stop-loss the sweep proved is
lethal. (Hyperliquid also caps most of the small alts this trades at 3–5× anyway, so the exchange
limit and the tail-survival limit converge.)

Keep the freed collateral as **account buffer for the correlated cluster** (~$400 per $100 notional
across the book) rather than deploying it — cross-margin cluster risk, not per-position margin, is
the real constraint.

---

## 5. Recommendations

1. **Keep the strategy as-is**: flat sizing, no price stop, 8h backstop, reclaim exit. The edge
   is real (reclaim P&L positive live and in-sample).
2. **Do not add a stop, trend gate, vol-scaling, or tighter concurrency cap** — all four degrade
   risk-adjusted return; the tail is inseparable from the edge.
3. **Set notional from a drawdown budget**, treating $100 notional as a ~−$400 MtM worst case.
4. **Leverage ≤ 3×** per position (~$33 margin/$100 notional); never exceed 5×.
5. **`MAX_POSITIONS=40` is a margin/liquidity cap, not a drawdown control** — don't lower it
   expecting less drawdown.
6. **Keep collecting live data** — 2 days / 86 trades in one regime is not yet significant; the
   question to watch is whether the live tail is fatter than the backtest assumed.

---

### Reproducibility

All figures produced by stdlib Python (no pandas) in [`analysis/`](analysis/), run from that
directory:

| Script | Produces |
|---|---|
| `cap_live.py trades_5m.csv trades_15m.csv` | §1 live P&L, §2a concurrency cap |
| `trend_gate.py` | §2b trend gate |
| `wide_stop.py` | §2c stop sweep (imported by the others) |
| `sizing_risk.py` | §3 flat vs vol-scaled dollar risk |
| `conc_cap.py` | §3 concurrency-cap tail sweep |
| `mae.py` | §4 MAE distribution + safe leverage |

*Not investment advice. In-sample study; signals are time-clustered and regime-dependent.*
