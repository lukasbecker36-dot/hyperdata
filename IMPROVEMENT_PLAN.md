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

## Phase 2 — Highest-value parameter changes (walk-forward, OOS-deflated only)

| # | Experiment | Rationale | Feasible now |
|---|---|---|---|
| 1 | Funding-extremity gate (sign → z>1.5 / top-decile) | Targets the "faded the rally too early" blowup | yes |
| 2 | OU half-life exits (fit θ, backstop = 1–2× half-life) | Principled answer to "is 8h right?" | yes |
| 3 | Log-volume z-score gate vs {3,4,5,7,10}× median | Normalizes crypto's fat volume tail | yes |
| 4 | Decoupled lookbacks (breakout/vol-median/RV separate) | Single 24h window is unmotivated | yes |
| 5 | RV cutoff sweep {50/60/70/80th}, trailing | Nagel: reversal pays most in high vol | yes |
| 6 | Liquidity-quintile monotonicity, exclude top bucket | Hardens the MID-tier edge | yes |

## Phase 3 — New signals / bigger builds (exploratory)

- Cross-sectional reversal overlay (rank-and-fade, market-neutral) — removes hidden BTC beta.
- Bollinger / RSI(2) alternative triggers + frequency sweep {1m,5m,15m,30m,1h}.
- VPIN / order-flow confirmation — **blocked**; start forward-logging the tape now.
- Stat-arb pairs sleeve — separate diversifying project.

## Adoption discipline

Every experiment carries a one-line hypothesis, an accept criterion (OOS-deflated Sharpe
improvement + t ≥ 3), and a kill criterion. Adopt sparingly to avoid the multiple-testing trap
the report warns about. Reserve the most recent ~6 weeks as an untouched final holdout.
