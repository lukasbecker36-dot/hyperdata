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

## Adoption discipline

Every experiment carries a one-line hypothesis, an accept criterion (OOS-deflated Sharpe
improvement + t ≥ 3), and a kill criterion. Adopt sparingly to avoid the multiple-testing trap
the report warns about. Reserve the most recent ~6 weeks as an untouched final holdout.
