# Improvement plan — testing the `claudeStudy.md` recommendations

A sequenced, gated plan to test the research report's ideas and adopt only what survives
honest evaluation. Every experiment reports an out-of-sample **Deflated Sharpe** and is
adopted only on OOS improvement, never in-sample.

## Framing (what the report couldn't know)

1. **Three of its risk recommendations we already tested — and they underperformed** on a
   concurrency-aware *dollar* basis (see `PAPER_TRADING_ANALYSIS.md`): vol-scaled sizing
   *worsened* maxDD (return/|DD| 0.89 vs flat 2.10); the same-direction concurrency cap was not
   robust (helped 5m, hurt 15m); every fixed-% wide stop destroyed the edge. Those tests were
   in-sample on the lookahead-contaminated signal set, so they get **re-adjudicated** under the
   Phase-0 harness — the ATR-scaled stop and correlation-aware cap are distinct enough to warrant
   a clean re-test.
2. **VPIN / order-flow is blocked on data.** The historical trade tape is not available via REST
   (`HANDOFF.md`) — only forward via WebSocket. It cannot be backtested now; we start logging the
   tape forward so it's testable later.
3. **The highest-value new lever ties to our live blowup.** Jul 19–20's losers were fade-shorts
   run over *during a rally* — we faded crowded longs too early. The **funding-extremity gate**
   (BIS "Crypto Carry") is the targeted fix: fade only when funding is *extreme*, not merely
   sign-matched.

## Phase 0 — Validity harness (gates everything)

Nothing downstream is trusted until this exists and the baseline survives it.

- Trailing/expanding quantiles everywhere (RV percentile, funding, liquidity) — removes the
  full-sample lookahead in `backtest.py` / `validate_stack.py` / `stop_target.py`.
- Walk-forward + purged K-fold with embargo (handles 8h-label time-clustering).
- Deflated Sharpe Ratio (Bailey–López de Prado) + block-bootstrap CIs.
- Re-baseline the current strategy through it.
- **Gate:** proceed only if the deflated OOS Sharpe stays comfortably positive (target > 1.5,
  t ≥ 3 per Harvey–Liu–Zhu). If it collapses, the edge was mostly lookahead.

Deliverable: `analysis/wf_harness.py`, baseline report.

### Phase 0 — RESULT (run on the 1h/8-month stacked signal set)

| Test | Result | Read |
|---|---|---|
| **Lookahead haircut** (full-sample vs causal trailing RV threshold) | +3.65 → **+3.67** (≈0) | The RV-threshold lookahead the report/README worried about is a **non-issue** — the threshold is stable, causal ≈ full-sample. |
| **PSR (edge > 0)** | **99.4%** | A positive edge almost certainly exists. |
| **Deflated Sharpe** (K=60 configs, corrects multiple-testing + fat tails) | **90.3%** | *Marginal* — just under the strict 95% bar. After accounting for config-space explored, ~10% chance it's selection. |
| **Block-bootstrap 95% CI** (annualized Sharpe) | **[+0.93, +6.85]** | Entirely positive, but wide; lower bound below the 1.5 target. |
| **Untouched 45-day holdout** | train +3.55 → **holdout +4.31** | Edge persists OOS on data never used to tune — strong contrary evidence to "it's just selection." |
| Deployed config (5×/0.6/8h) in-sample rank | **16 / 60** (below median) | Not cherry-picked — the true selection bias is *smaller* than DSR assumes, so the real evidence is a bit stronger than 90%. |

**Verdict: CONDITIONAL PASS.** The edge is real and OOS-persistent, and it is **not** primarily a
lookahead artifact (haircut ≈ 0) — contrary to the README's own caveat. The remaining risk is
**multiple testing**: it sits just under the strict deflated-Sharpe bar. So proceed to Phases 1–2,
but with hard discipline — the config space is already mined, every new lever must clear a high
OOS bar, and we must not keep adding trials. Do not scale capital on the Sharpe-3.8 figure; the
honest number is ~3.7 annualized with a wide CI whose lower bound is ~0.9.

## Phase 1 — Re-adjudicate the three risk levers under the honest harness

Vol-scaled sizing · correlation/same-direction concurrency cap · ATR (3–5× ATR) catastrophe-only
stop. Adopt only on OOS-deflated improvement. Prior: first two likely fail on dollar/DD; the
ATR-scaled stop gets a real shot (places the stop wider on the high-vol names where the tail lives).

### Phase 1 — RESULT (causal series, 2,330 trades, 45d holdout)

Baseline: annualized Sharpe **+3.67**, holdout **+4.31**, total +$704, maxDD −$300, worst −$38,
return/|DD| **2.35**.

| Lever | Sharpe | holdout | total | maxDD | worst trade | return/\|DD\| | Verdict |
|---|---|---|---|---|---|---|---|
| **baseline** | +3.67 | +4.31 | +$704 | −$300 | −$38 | 2.35 | — |
| Vol-scaled sizing (1/rv, 4×) | +3.13 | +3.19 | +$403 | −$302 | −$25 | 1.34 | **reject** — halves return, worse Sharpe & r/DD; only shrinks the worst *single* trade |
| Same-dir cap 8 (shorts) | +3.17 | +4.07 | +$579 | −$274 | −$38 | 2.11 | **reject** — best of the caps, but trades Sharpe for a modest DD cut; flat sizing does it better |
| Same-dir cap ≤5 / BOTH | +0.8→+2.7 | −0.1→+2.8 | worse | — | −$38 | <2.1 | **reject** — degrades sharply, holdout even goes negative |
| ATR stop 5×ATR | +1.65 | +2.38 | **−$208** | **−$596** | −$29 | −0.35 | **reject** — destroys return *and deepens* maxDD |
| ATR stop 3–4×ATR | +1.4→1.6 | +1.3→2.8 | −$580→−$341 | −$786→−$700 | −$17→−$23 | negative | **reject** — same failure, worse |

**Verdict: reject all three.** None improves OOS risk-adjusted return.
- **Vol-scaling** shrinks the worst single trade (−$38→−$25) but halves return and cuts Sharpe/r-DD
  — it down-weights the high-vol names that carry the edge, and doesn't reduce maxDD.
- **Concurrency caps** only reduce maxDD modestly (cap-8 shorts: −300→−274) at a Sharpe cost;
  tighter caps and two-sided caps collapse. Flat-notional scaling achieves DD reduction more
  efficiently (linear, Sharpe-preserving).
- **The ATR stop is the sharpest rejection**: at *every* k it not only kills return but **deepens**
  the drawdown, because it converts reverting overshoots into locked losses that bleed the equity
  curve — worse than one occasional tail loss. ATR-scaling does not rescue the stop; the stop
  mechanism itself sells the overshoot bottom regardless of how it's scaled.

Confirms `PAPER_TRADING_ANALYSIS.md` under the honest harness: **the only risk control that survives
is flat-notional sizing** — pick the notional for your drawdown budget; don't filter the tail.

## Phase 2 — Highest-value parameter changes (walk-forward, OOS-deflated only)

| # | Experiment | Rationale | Feasible now |
|---|---|---|---|
| 1 | Funding-extremity gate (sign → z>1.5 / top-decile) | Targets the "faded the rally too early" blowup | yes |
| 2 | OU half-life exits (fit θ, backstop = 1–2× half-life) | Principled answer to "is 8h right?" | yes |
| 3 | Log-volume z-score gate vs {3,4,5,7,10}× median | Normalizes crypto's fat volume tail | yes |
| 4 | Decoupled lookbacks (breakout/vol-median/RV separate) | Single 24h window is unmotivated | yes |
| 5 | RV cutoff sweep {50/60/70/80th}, trailing | Nagel: reversal pays most in high vol | yes |
| 6 | Liquidity-quintile monotonicity, exclude top bucket | Hardens the MID-tier edge | yes |

### Phase 2 — RESULT (causal series, baseline holdout Sharpe +4.31, r/DD 2.35)

| Lever | best config | Sharpe | holdout | total | maxDD | r/DD | Verdict |
|---|---|---|---|---|---|---|---|
| **E) Liquidity** | **MID tier only** | +4.64 | **+6.59** | +$601 | −$147 | **4.10** | **ADOPT** |
| B) RV cutoff | 70th pct | +4.22 | +3.52 | +$706 | −$235 | 3.00 | consider (risk-eff) |
| D) Volume mult | 4× ≈ 5× | +4.64 | +4.28 | +$854 | −$319 | 2.68 | keep 5× (4×≈5× OOS) |
| C) Hold | 8h | +3.67 | +4.31 | +$704 | −$300 | 2.35 | keep 8h |
| **A) Funding extremity** | \|z\|≥1.5 | −0.97 | **−2.25** | +$112 | −$92 | 1.22 | **REJECT** |

**A) Funding-extremity gate — REJECTED (the report's headline, and a real surprise).** Tightening
from sign-match to \|funding z\| ≥ 1 / 1.5 / 2 drives the **holdout Sharpe negative** (+4.31 →
−0.06 → −2.25 → −2.31). Per-trade bps rise (+61 at z≥2) but OOS collapses. The mechanism is exactly
our Jul 19–20 blowup: **extreme funding means the crowded trend still has fuel, so the fade gets run
over** — conditioning on extremity concentrates into the continuation regime that kills the strategy.
The report's own caveat ("funding can stay extreme through strong trends; fading too early loses")
is what the data shows. Keep the sign-match; do **not** add extremity.

**E) Liquidity concentration — ADOPT (the strongest, most theory-consistent result).** The HIGH tier
is nearly worthless OOS (holdout **+0.62**, r/DD 0.31); the MID tier is excellent (holdout **+6.59**,
r/DD **4.10**). By notional-volume quintile the edge is monotone: lowest-liquidity eligible names
earn +79 bps/trade (r/DD 4.38) while the top quintile is **dead** (+0.1 bps, r/DD 0.00). This matches
Liu–Tsyvinski–Wu (big coins show momentum, not reversal) exactly. **Drop the HIGH tier — trade MID
only.** It roughly halves trade count while raising OOS Sharpe and cutting maxDD by half.

**B) RV cutoff — promising for risk efficiency.** 70th pct keeps the same total return ($706 vs $704)
with maxDD cut 22% (−300 → −235), r/DD 2.35 → 3.00 — but holdout Sharpe dips (+4.31 → +3.52). A
risk-efficiency gain (Nagel's "push the vol cutoff harder"), worth validating on more holdout.

**C) Hold / OU — keep 8h.** 8h is the r/DD optimum. Shorter holds (4–6h) give higher *holdout Sharpe*
(+5.5–5.9) but poor r/DD (0.5–0.8); longer holds add total return but worse Sharpe/DD. Note: the
pooled **OU half-life came out ~27h**, which would (per the report's "1–2× half-life" rule) argue for
much longer holds — but that's misleading here (the fat continuation tail inflates the reversion-time
estimate), and the risk-adjusted optimum is clearly ~8h. A case where the OU heuristic would mislead.

**Phase 2 output config to forward-test: MID-tier-only** (the one robust win), optionally with the
70th-pct RV cutoff for extra risk efficiency. Both are independently theory-supported, so testing the
combination is disciplined, not mining. This is a one-line universe change in `paper_bot.py`
(`tier in ('HIGH','MID')` → `tier == 'MID'`).

### 24h % display-artifact roll-off — RESULT (`analysis/rolloff_24h.py`) — REJECT

"Idiot edge": the candle exactly 24h ago rolls off the displayed 24h% window; big green rolling off
makes 24h% drop (naive sell), big red makes it rise (naive buy). Traded cross-sectionally (long
biggest-red-24h-ago / short biggest-green, hold H). Raw edge is tiny (+0.17 bps/hourly, ~half the
thread's Binance claim) and dies on turnover cost (−25 Sharpe @5bp at 1h); only 24h-hold scrapes
+0.38 Sharpe @5bp. **Placebo kills it:** keying off the 12h-ago candle is *stronger* than the 24h-ago
one at 3h/6h holds — so the whisper of edge is generic short-horizon reversal, NOT a 24h-specific
display artifact. (Tested the public teaser mechanism; refined rules are paywalled. The 900%/4yr
headline is full-position-size compounding, not a Sharpe.)

### 24h roll-off — REFINED (`analysis/refined_rolloff.py`) — real but sub-scale, NOT tradeable here

Guessed the "refined" version and built it: (1) **leaderboard salience** — only trade coins whose
*current* displayed 24h% is an extreme *caused by* the about-to-roll-off candle (sign(r_roll)==sign(disp)
and |r_roll| ≥ share·|disp|); that is the exact ticker number retail watches move. (2) magnitude gate on
the roll candle. (3) **orthogonalize r_roll vs the recent 6h return** to strip the generic reversal the
naive placebo exposed. (4) short hold, precise roll timing.

This is a materially better result than the naive one:
- **Passes the placebo.** On the refined pipeline, lag=24 now *beats* lag=12 at 1h and 3h holds
  (1h: annSh +1.88 vs −3.23; 3h: +1.22 vs −0.50). The salience gate isolated something genuinely
  24h-specific — the ablation shows salience is the whole lever (gross 0.06 → 3.68 bps at 3h), magnitude
  gate does nothing, orthogonalization trims gross slightly (honest, removes reversal).
- **But it doesn't clear the economic bar.** Best config (3h hold, share≥0.8, dec 0.1) fires only ~145–244
  times over 8mo (salience is rare), so annualizing on *actual* trade frequency (not the 3h grid) gives
  gross Sharpe ~1.5 with a per-rebalance mean t-stat of only ~1.1 (**not significant**, need |t|>2).
  Net Sharpe ~0.6–0.9 @3bp and ~0.3–0.6 @5bp; holdout unstable across configs (+5.9, −0.1, +4.5, −10.2
  bps). Un-haircut for ~30 configs tried (Deflated-Sharpe would trim further).

Verdict: the mechanism is **real and 24h-specific** (refinement rescued it from the naive placebo
failure), but on Hyperliquid perps it's a ~0.5-Sharpe-net, statistically-insignificant whisper — too
small and too noisy to deploy. Plausibly stronger on retail-heavy *spot* venues where the display-artifact
audience is larger; this perp dataset doesn't support trading it.

### Lead-lag (BTC leads alts) — RESULT (`analysis/lead_lag.py`) — REJECT (real but not tradeable)

Trade the partial-adjustment gap: long alts that lagged BTC's move (gap = beta·r_btc − r_alt),
short over-shooters, market-neutral, hold H bars. The effect is **real** — gross positive at every
horizon (+1.8 to +5.1 bps/rebalance, gross Sharpe +4 to +9), laggards do catch up. But the edge is
tiny with huge turnover (full decile basket every 1–6h): even 5 bps/rebalance kills 1–3h holds; 6h
only breaks even (+0.07, holdout +2.76); 15 bps is deeply negative everywhere. Finer timeframes would
worsen it (turnover cost, not signal, is the binding constraint). Same verdict as XS-momentum /
stat-arb / carry: a genuine signal too thin to survive costs.

### Clustered vs isolated entries — RESULT (`analysis/cluster_entries.py`) — clustering is the EDGE, do NOT cap

Hypothesis: when a wide market move fires many same-side fades at once, those correlated entries are
worse than isolated single entries, so a rate limit / mix cap would help. **The data says the exact
opposite.** Bucketing baseline fade net by trailing same-side crowd (causal, knowable at entry):

  same-side 1h crowd:  isolated(1) **−19.1 bps** (t=−1.0)  →  2: +31.1  →  3: +31.9  →  **4+: +56.5 (t=4.1)**

The entire positive expectancy lives in the clustered entries; **isolated single breakouts have
*negative* expectancy.** Same shape at 3h/6h windows and by directional imbalance (most-lopsided burst
= most profitable). Mechanism: a synchronized multi-coin break IS the capitulation/exhaustion event
that mean-reverts; a lone breakout while the rest of the market is calm is more likely real
idiosyncratic news/trend, so the fade fails. This is the same fact the HMM found (edge lives in the
high-vol/stress regime) — a cluster is the micro-signature of that regime.

**Monthly-robust** (the test that retracted the MA filter): clustered beats isolated in **all 8 months**,
gap +15.6 to +126.4 bps, never negative. Not a single-month artifact.

Both proposed rules therefore *hurt*. A same-side rate limit (skip if ≥cap same-side entries in a
trailing window) was tested at cap∈{2,3,4} × window∈{3,6,12}h: every variant is worse than baseline —
cum $ falls from **+647 → +72 or below**, ret/DD from **2.10 → ≤0.33**, because you skip exactly the
crowded entries that carry the edge. The tail improves only slightly (worst-48h −253 → ~−180) — a
terrible trade for ~90% of the profit.

Caveat for the legitimate underlying worry (correlated exposure stacking): the reason to bound a
cluster is a **capital/margin** limit (can't fund N simultaneous $100 positions), **not** an edge
reason. If capital-constrained, size *all* entries down uniformly (preserves the edge proportionally)
rather than skipping the crowded ones — or accept you're dropping your best trades. `conc_cap.py`
covers the peak-deployed-capital view of that.

### Slot cap sizing — RESULT (`analysis/slot_sweep.py`) — the 5-slot cap throttles ~88% of the edge

Follows the clustered-entry finding: since crowded bursts carry the edge, does the live 5-slot cap cost
us? Swept the concurrency cap (total and per-side) at live sizing ($25 base, 3x → ~$8.3 margin/slot),
pricing P&L, drawdown, peak margin, and the share of the clustered-4+ edge that survives:

  TOTAL cap  5:  +$20  maxDD −58  ret/DD 0.35  peak 5   margin $42   clustered-edge kept **5%**
  TOTAL cap 10:  +$55  maxDD −62  ret/DD 0.88  peak 10  margin $83   kept 15%
  TOTAL cap 20:  +$69  maxDD −71  ret/DD 0.97  peak 20  margin $167  kept 35%
  TOTAL cap ∞:  +$162  maxDD −77  ret/DD **2.10** peak 82 margin $683 kept 100%

Two things jump out. (1) **maxDD is nearly flat across every cap** (−$53 to −$77): the drawdown is driven
by bad individual trades/periods, *not* by concurrency stacking. So capping removes return without
removing risk → **ret/DD is monotonically better uncapped** (2.10 vs ≤0.97 at any finite cap). There is
no risk argument for the cap. (2) 5 slots captures only ~12% of total P&L and **5% of the clustered
edge** — the strategy's expectancy *needs* to hold many concurrent positions through a burst (peak
concurrency 82 on hold-to-backstop; lower live because reclaim frees slots faster).

The binding constraint is **capital, not correlation.** Set slots from free margin: slots ≈
free_margin / $8.3 (more for ats-3x names). A PER-SIDE cap beats a total cap at equal number (holds both
legs through a burst: per-side 5 = +$45 vs total 5 = +$20). **Paper action:** uncap now (MAX_POSITIONS
40+) — the 5-slot paper cap is understating live paper P&L by ~8x and should be lifted for free.

### Live-accurate frontier — RESULT (`analysis/reclaim_frontier.py`) — the cap is a risk AMPLIFIER

Re-ran with the *actual* live rules — ATS sizing ($25 base × 0.5–3×), reclaim exits, and 3× isolated
liquidation — instead of flat $25 / hold-to-backstop. Avg hold falls to **5.2h** (54% reclaim, 45%
backstop, 0.7% liq). The result is worse than the flat version and inverts the risk story:

  TOTAL cap  5:  **−$70**  maxDD −157  ret/DD −0.45  peakMargin $104   clustered kept 19%
  TOTAL cap 15:  −$5   maxDD −116  ret/DD −0.04  peakMargin $232   kept 52%
  TOTAL cap 20:  +$8   maxDD −111  ret/DD  0.08  peakMargin $299   kept 60%
  TOTAL cap ∞:  **+$94** maxDD **−107** ret/DD **0.88** peakMargin $864  kept 100%

Under live rules the 5-slot cap makes the strategy **lose money**, and — critically — **maxDD is *larger*
capped (−$157) than uncapped (−$107) despite deploying ~8× less capital.** The cap doesn't reduce risk,
it concentrates it. Mechanism: reclaim makes **winners exit fast (<5h) and losers run to the 8h
backstop**, so at any instant the slots are occupied by not-yet-reclaimed losers while fresh clustered
*winners* are blocked — adverse selection of exactly which trades you skip (clu4+ kept only 19% at cap5).
Breadth across the burst is the edge; a slot cap kills breadth and keeps the laggards.

**Recommendation:** the correct lever is **per-name size, not slot count.** ret/DD (0.88) is a property of
breadth and is invariant to per-name notional, so if capital-constrained, *shrink BASE* to fit more names
(e.g. $10 base → peak margin ~$345, same ret/DD) rather than capping slots. Keep a high per-side cap
(≥20) only as a runaway backstop. Paper: uncap immediately — the live paper arms are currently measuring
a cap-clogged book that shows a loss where the real strategy is +$94. True worst-burst margin to run
uncapped is ~$864 at $25 base/3× (rarely hit; avg concurrency ≪ 81 peak).

### HMM regime study — RESULT (`analysis/hmm_regime.py`) — diagnostic YES, optimization lever NO

Fit a stdlib Gaussian HMM on a market-level observation series [BTC hourly return, log market-vol
index], then bucketed the strategy's *actual* signals by the regime knowable **at entry**. Two
disciplines because regime conditioning here has been a single-month artifact before (946914b):
causal **filtered** (forward-only) labels fit on the first half only, and a **month-by-month** gap test.

- **Diagnostic (real, expected):** the fade is a high-volatility strategy. In-sample it is stark —
  K=2 STRESS +46.0 bps/trade (t=3.81) vs CALM −10.3; K=3 HIGH-vol +72.6 (t=4.93) vs LOW −0.4 / MID −5.4.
  All the P&L lives in the stress/high-vol state. This makes sense (a volume-exhaustion fade needs vol
  to have something to revert) and it essentially **re-derives the rv≥60th-pct gate already in the
  strategy** — not new alpha.
- **Optimization lever (fails causally):** the clean in-sample edge does **not** survive forward-only
  labeling. K=3 OOS collapses to LOW +17.4 / MID +21.5 / HIGH +16.9, all insignificant (t≈0.6–1.1) —
  the t=4.9 HIGH-vol edge was Viterbi lookahead. K=2 OOS keeps a directional STRESS>CALM (+25.8 vs
  +10.4) but neither is significant and the monthly gap flips hard in June-2026 (CALM +212.7 vs STRESS
  −9.0, a −222 bps month). Better distributed than the retracted MA filter (positive gap in 6/8 months,
  not one-month) but not monotone or significant enough to lever.
- **Capital-efficiency math:** gating to STRESS-only OOS lifts per-trade net +18.9→+25.8 bps (+37%) but
  drops ~45% of trades, so **total** OOS P&L falls (+190% vs +252% trading everything). You pay real
  P&L for a per-trade improvement that isn't OOS-significant.

Verdict: worth knowing (the edge is concentrated in high-vol regimes — a robustness fact, and a reason
the deployed vol gate is right), **not** worth a new HMM regime-gating overlay. It re-derives the
existing filter in-sample and adds no robust causal value beyond it. Same recurring lesson as the
retracted MA regime filter.

### Funding CARRY — RESULT (`analysis/carry.py`) — REJECT

First test to count the funding *cashflow* as income (all prior tests were price-return only).
Cross-sectional funding sort: short top-decile funding (collect), long bottom-decile, held H hours,
market-neutral, decomposed into price vs funding components.

| hold | PRICE bps | FUND bps | TOTAL bps |
|---|---|---|---|
| 8h | −52.4 | +3.4 | −49.0 |
| 168h (1w) | −240.9 | +50.0 | −190.9 |

Funding income is real and scales with hold, but the **adverse price move dwarfs it 4–5×** at every
horizon — you're paid the carry because the crowded side keeps winning short-term. The funding-sorted
long-short basket is a **momentum/beta bet in disguise** (high-funding = pumping high-beta alts), so it
just expresses "short the winners" and bleeds in a trending regime; the funding cashflow is a rounding
error on that price tilt (the ±30 Sharpes are regime artifacts, not alpha). Harvesting carry cleanly
needs a **delta-neutral spot hedge** (no spot data here), and even then income is ~0.1 bps/hr. (Note:
one dataset gotcha fixed here — funding timestamps aren't hour-aligned to candles, so accrue by time
window, not exact-ms match.)

## Phase 3 — New signals / bigger builds (exploratory)

- Cross-sectional reversal overlay (rank-and-fade, market-neutral) — removes hidden BTC beta.
- Bollinger / RSI(2) alternative triggers + frequency sweep {1m,5m,15m,30m,1h}.
- VPIN / order-flow confirmation — **blocked**; start forward-logging the tape now.
- Stat-arb pairs sleeve — separate diversifying project.

### Phase 3 — RESULT (causal series, baseline holdout Sharpe +4.31, r/DD 2.35)

| Construction | best config | Sharpe | holdout | total | maxDD | r/DD | Verdict |
|---|---|---|---|---|---|---|---|
| **A) Bollinger / price z-score** (replace breakout) | **\|z\|≥2.5** | **+5.36** | **+6.46** | +$1040 | −$214 | **4.85** | **PROMISING** |
| A) Bollinger as extra gate on breakout | +\|z\|≥2 | +4.33 | +4.75 | +$737 | −$299 | 2.47 | marginal |
| B) Volume log-z-score (replace 5×) | z≥2 | +4.28 | +5.62 | +$694 | −$299 | 2.32 | neutral — ≈ 5× median |
| C) RSI(2) extreme | 95/5 | +3.36 | +3.92 | +$648 | −$424 | 1.53 | reject — worse |
| D) Cross-sectional reversal | any lookback | −2.9 | **−6.4** | −$51 | −$62 | neg | **reject — negative** |

**A) Bollinger / price z-score — the one real win.** Fading a **\|z\| ≥ 2.5** stretch (price vs 20-bar
MA) *as a replacement for the range-breakout* dominates the baseline on every axis: Sharpe +5.36 vs
+3.67, **holdout +6.46 vs +4.31**, same total return, and r/DD **4.85 vs 2.35** with a smaller maxDD.
This is believable because it's a *refinement of the same edge* (the report's point: Bollinger is the
continuous version of the discrete range-pierce), not a new factor — the z-score trigger is cleaner
than a raw marginal new-high. **But** it's a selected config after many trials, so it does not get
adopted off a backtest — it gets **forward-tested as a paper arm** (like MID-only) before trusting it.

**B) Volume log-z-score — neutral.** z≥2 is ~indistinguishable from the 5× median (holdout a touch
better, r/DD the same). The report's "normalization tightens the signal" doesn't show up here — keep
5× median (simpler), or z≥2 as a wash. No adopt.

**C) RSI(2) — reject.** Generates far more, lower-quality signals (+11–15 bps/trade) with worse Sharpe
and r/DD than the breakout. Not a useful trigger here.

**D) Cross-sectional reversal — reject, and it contradicts the report.** Ranking the universe by past
return and fading the extremes (market-neutral) is **negative at every lookback** (holdout −6.4). At
the 8h horizon Hyperliquid's biggest movers show **continuation, not reversal** — consistent with
Liu–Tsyvinski–Wu's point that the largest movers carry momentum. The report's "cross-sectional is
often more robust" does not hold on this data/horizon.

**Not run:** VPIN/order-flow (needs the historical trade tape — unavailable), stat-arb pairs (separate
project), frequency sweep (only 1h has the full 8-month window).

**Phase 3 output:** one candidate to forward-test — the **Bollinger \|z\|≥2.5 trigger** — as a paper
arm, judged live before any adoption. Everything else stays as-is.

### Stat-arb sleeve — RESULT (market-neutral, Avellaneda–Lee OU s-score; `analysis/stat_arb.py`)

Residualize each coin's returns on a market factor, model the idiosyncratic cumulative residual as
OU, trade the s-score. Causal, 45d holdout, 12 bps round-trip over 2 legs.

| open \|z\| | trades | gross bps | net bps | ann Sharpe | holdout | verdict |
|---|---|---|---|---|---|---|
| 1.25 (AL default) | 9258 | +3.8 | −8.2 | −1.80 | −3.89 | costs kill it |
| 2.0 | 2493 | +7.3 | −4.7 | −0.16 | −3.38 | negative |
| 2.5 | 562 | +19.7 | +7.7 | −0.06 | −3.00 | net+ but holdout negative |
| 3.0 | 81 | +75.2 | +63.2 | +5.60 | +6.31 | un-tradeable (~10/yr, noise) |

**Verdict: do NOT build the sleeve.** Gross edge rises monotonically with dislocation size (extreme
residuals revert harder), but at deployable frequency it's net-negative after two-leg costs and the
recent holdout is negative — a decayed edge (matches the report's "crypto carry Sharpe turned
negative in 2025"). Only the |z|≥3 tail clears costs, at ~10 trades/year = statistically meaningless.
Third reversal-flavored idea to fail on HL perps (after cross-sectional reversal and funding
extremity): at these horizons the big/idiosyncratic movers **continue** more than they revert.
Untested refinement: full PCA multi-factor residuals — but priors argue against it rescuing the edge.

### Continuation as a strategy — RESULT (`cont_momentum.py`, `ts_continuation.py`)

Tested because every failed reversal idea *implies* continuation. Two forms, both rejected:

- **Cross-sectional momentum** (long winners / short losers, 24h–2w lookbacks/holds): gross negative
  at almost every horizon, all Sharpes negative after cost, holdouts deeply negative. So the
  cross-sectional dimension carries **no cost-surviving factor in either direction** — reversal AND
  momentum fail; it's not that "continuation dominates."
- **Time-series breakout continuation** (ride the 24h breakout, incl. HIGH-liquidity where the fade
  doesn't fire): net **−13 to −40 bps/trade** across all tiers/horizons, negative holdouts. Riding
  breakouts loses even in the liquid names.

**Verdict: do NOT build a continuation book.** Neither broad reversal nor broad continuation clears
costs on HL perps. The ONLY real edge is the narrow, multi-gated **volume-exhaustion fade in MID
names** — breakouts on average go nowhere net of cost *except* in the specific exhaustion regime the
fade identifies (the continuation of vol-spike breakouts in MID loses −33 bps, the strong mirror of
the fade's strength there — confirming the edge is genuinely conditional, not a broad factor).
Momentum would also add its own crash-tail risk. Stop adding strategies; run the validated fade arms.

### Whale-vs-crowd (avg trade size) — RESULT (`analysis/avg_trade_size.py`) — PROMISING

Uses `num_trades` (never used before). Decompose each 5× spike into trade-count vs avg-trade-size
(v/n), normalized per-coin vs trailing 24h. Hypothesis was "crowd spikes = exhaustion → fade";
data says the **opposite** — the fade is strongest on **high avg-trade-size** spikes (few big
aggressive trades):

| avg-trade-size quartile | net/trade | win% | holdout Sh |
|---|---|---|---|
| Q1 (crowd/small) | +9.7 bps | 55% | +1.34 |
| Q4 (whale/big) | **+72.8 bps** | 60% | +4.67 |

Mechanistic sense: a single large aggressive order piercing the range is Nagel's "urgent
price-pressuring flow" that overshoots and reverts hardest (not the VPIN "informed flow continues"
case, which is about *sustained* one-sided flow). Both spike dimensions point the same way — the
more extreme the spike, the stronger the fade.

**Status: promising, not adopted.** Restricting to Q4 ~triples per-trade edge (+27→+73 bps) but the
holdout Sharpe only holds at baseline (fewer trades → noisier daily series). So it's a **conviction /
sizing** signal (bet bigger on high-avg-trade-size spikes) more than a Sharpe lift, and it's a
selected quartile — forward-test (as an arm or a size multiplier) before trusting it. First *new*
lever in a while with a real, mechanistically-sensible gradient rather than a reversal/continuation rehash.

**Full sized-equity backtest** (`analysis/ats_equity.py`, notional = 100·clip(ats/2, 0.5, 3.0),
same breakout/HIGH+MID entries as flat):

| metric | flat $100 | ats-sized |
|---|---|---|
| total P&L | +$660 | +$1038 |
| daily $ Sharpe | +1.61 | +2.04 |
| holdout Sharpe (45d) | +1.45 | +1.48 |
| maxDD | −$298 | −$256 |
| **return/\|maxDD\|** | 2.21 | **4.06** |
| avg notional / peak margin | $100 / $2667 | $131 / $3506 |

Sizing lifts Sharpe (1.61→2.04) and nearly doubles return/|maxDD| (2.21→4.06) with a *smaller*
drawdown despite +31% capital — the up-sized (whale) trades were disproportionately winners, so the
reweighting carries real information (a trade-specific reweight can't raise $ Sharpe otherwise). The
45d holdout is flat (1.45→1.48), so it's **promising in-sample, unconfirmed OOS** — the live `15m-ats`
arm is the test. Uses daily dollar-P&L Sharpe (portfolio-realistic), not comparable to the
daily-mean-return Sharpe elsewhere; compare ats-vs-flat within this table only.

**ATS x {trigger, universe}** (`analysis/ats_combos.py`, ats vs flat, return/DD = scale-free):

| config | flat ret/DD | ats ret/DD | flat holdout | ats holdout |
|---|---|---|---|---|
| ats (breakout HM) | 2.21 | **4.06** | +1.45 | +1.48 |
| ats+mid | 3.30 | **2.98** | +5.21 | +3.89 |
| ats+boll | 4.62 | **6.73** | +5.16 | +4.25 |
| ats+mid+boll | 4.50 | 5.69 (maxDD −96→−111) | +5.28 | +4.50 |

ATS sizing **helps in the broad HIGH+MID universe** (breakout & boll: return/DD up, DD flat-to-smaller)
but **hurts stacked on MID-only** (ats+mid: return/DD 3.30→2.98, maxDD −158→−254) — MID already
concentrates into few selective trades, so up-sizing over-concentrates the tail; the two concentration
levers are redundant/conflicting. And the **45d holdout does not confirm ats in any config** (flat in
base, lower in every combo) — the gains are in-sample. So the deployed `15m-ats` arm (breakout HM +
ats) is the best ats config; do NOT build ats+mid arms. Live arm is the deciding test.

## Adoption discipline

Every experiment carries a one-line hypothesis, an accept criterion (OOS-deflated Sharpe
improvement + t ≥ 3), and a kill criterion. Adopt sparingly to avoid the multiple-testing trap
the report warns about. Reserve the most recent ~6 weeks as an untouched final holdout.
