#!/usr/bin/env python3
"""ROI if collateral stays where it is. Enforces the ceiling instead of ignoring it.

config_forecast.py showed peak margin at $664 against $383 of spot, i.e. the current
config wants more collateral than the account has. That report left the ceiling unenforced,
so its P&L assumed every signal got funded. If funding is held at today's level the
exchange simply rejects the orders that do not fit, and those signals are lost -- which
means the honest ROI has to be computed with the ceiling in place, not by dividing an
unconstrained P&L by the smaller balance.

Two arrival rates, because it matters and the truth is between them:
  full event rate       47.7 gated signals/day from the 15m event set
  thinned to live 0.54  the 23.2 fills/day actually observed, after the 13% miss rate,
                        cap refusals and period differences

Three bps assumptions, spanning the real uncertainty:
  backtest +44.0    frictionless candle exits
  live ex-liq +21.7  304 live fills excluding the three liquidations
  live all-in +2.5   all 307 live fills

  python3 analysis/roi_at_current_funding.py
"""
import math, os, sys
import numpy as np
import pandas as pd

SPOT, USABLE = 383.0, 0.90
LIVE_EXLIQ, LIVE_ALL = 21.7, 2.5

src = open(os.path.join(os.path.dirname(__file__) or ".", "config_forecast.py"),
           encoding="utf-8").read()
exec(src.split("# ---- book simulation")[0])          # noqa: S102 -- reuse event build


def simulate(margin_cap, thin=1.0, seed=7):
    """Book simulation with a hard collateral ceiling.

    Order of checks mirrors the bot: position/side/gross/daily-loss first, then margin,
    because the exchange rejection is what happens after the bot has already decided to
    trade. thin<1 drops signals at random to reproduce the live arrival rate.
    """
    rng = np.random.default_rng(seed)
    op, took, ser = [], [], []
    ref_m = ref_o = 0
    dp, cd = 0.0, None
    for r in ev.itertuples():
        if thin < 1.0 and rng.random() > thin:
            continue
        op = [q for q in op if q[0] > r.t]
        d = pd.to_datetime(r.t, unit="ms").date()
        if d != cd:
            cd, dp = d, 0.0
        gross = sum(q[2] for q in op)
        marg = sum(q[3] for q in op)
        same = sum(1 for q in op if q[1] == r.dirn)
        if (len(op) >= MAX_POS or same >= MAX_SIDE or gross + r.ntl > MAX_GROSS
                or dp <= -DAILY_LOSS):
            ref_o += 1
            continue
        if marg + r.margin > margin_cap:
            ref_m += 1
            continue
        dp += r.ntl*r.net/1e4
        took.append(dict(net=r.net, ntl=r.ntl, pnl=r.ntl*r.net/1e4))
        op.append((r.t + r.bars*900000, r.dirn, r.ntl, r.margin))
        ser.append((sum(q[3] for q in op), len(op)))
    return (pd.DataFrame(took), pd.DataFrame(ser, columns=["margin", "npos"]),
            ref_m, ref_o)


cap = SPOT*USABLE
print(f"\n{'='*76}")
print(f"### FUNDING HELD AT ${SPOT:.0f} SPOT — {USABLE:.0%} usable = ${cap:.0f} margin ceiling")
print("=" * 76)
print(f"  {'arrival':<24} {'trades':>7} {'lost:margin':>12} {'lost:caps':>10} "
      f"{'peak marg':>10} {'avg marg':>9} {'peak pos':>9}")
runs = {}
for lab, thin in (("full event rate", 1.0), ("thinned to live (0.54)", 0.54)):
    TT, SS, rm, ro = simulate(cap, thin)
    runs[lab] = (TT, SS, rm, ro)
    print(f"  {lab:<24} {len(TT):>7,} {rm:>12,} {ro:>10,} "
          f"${SS.margin.max():>9.0f} ${SS.margin.mean():>8.0f} {int(SS.npos.max()):>9}")

print(f"\n  ROI on the ${SPOT:.0f} you actually have:")
print(f"  {'arrival':<24} {'bps basis':<22} {'$/30d':>9} {'ROI/30d':>9} {'ROI ann':>9}")
for lab, (TT, SS, rm, ro) in runs.items():
    bt = float(ev.net.mean())        # mu_bps lives after the split point
    for bl, bps in ((f"backtest {bt:+.1f}", bt),
                    ("live ex-liq +21.7", LIVE_EXLIQ),
                    ("live all-in +2.5", LIVE_ALL)):
        p30 = len(TT)*bps/1e4*TT.ntl.mean()/days*30
        print(f"  {lab:<24} {bl:<22} {p30:>+9.2f} {100*p30/SPOT:>+8.1f}% "
              f"{100*p30/SPOT*12:>+8.0f}%")
    print()

print("  Margin refusals are already priced in above: those signals are not taken, so the")
print("  ROI is what the ceiling permits rather than what the strategy would earn unfunded.")
print(f"\n  Sensitivity to the usable fraction, at the live arrival rate and +21.7 bps:")
for u in (0.80, 0.90, 1.00):
    TT, SS, rm, ro = simulate(SPOT*u, 0.54)
    p30 = len(TT)*LIVE_EXLIQ/1e4*TT.ntl.mean()/days*30
    print(f"    {u:.0%} usable (${SPOT*u:>6.0f})  {len(TT):>5,} trades, "
          f"{rm:>4,} lost to margin  ->  ${p30:>+7.2f}/30d  {100*p30/SPOT:>+6.1f}%")
