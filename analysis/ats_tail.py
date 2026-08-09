#!/usr/bin/env python3
"""WHERE does ats sizing earn, if it earns? The mean hides the mechanism.

ats_verdict.py finds corr(ats multiplier, net_bps) = +0.138 on live fills (t=+1.86) and
+0.065 on paper (t=+1.23) -- both positive, neither conclusive, and 94% the same signals,
so it is one weak result rather than two agreeing ones.

But the eight worst live trades carry ats_ratio 0.85 to 2.71, and five of the eight would
have been sized DOWN. That suggests the rule's value is not "bet more on winners" at all
-- it is "bet less on disasters". Those are very different claims:

  a MEAN effect needs the multiplier to track expected return, and is fragile because
  expected return is nearly unmeasurable at this sample size
  a TAIL effect only needs low-ats trades to blow up more often, which is a much coarser
  and more robustly estimable property -- and it is worth having even if the mean effect
  is zero, because the strategy has no stop-loss and its risk IS the left tail

So: split the outcome distribution rather than averaging it.

  python3 analysis/ats_tail.py [live.csv] [paper.csv]
"""
import csv, math, sys
from datetime import datetime

LIVE = sys.argv[1] if len(sys.argv) > 1 else "live_15m_ats/trades_15m.csv"
PAPER = sys.argv[2] if len(sys.argv) > 2 else "paper_15m_ats/trades_15m.csv"
SIZE_REF, SIZE_MIN, SIZE_MAX = 2.0, 0.5, 3.0


def load(path, base, ats_col):
    out = []
    for r in csv.DictReader(open(path)):
        try:
            net = float(r["net_bps"])
            if abs(net) < 1e-9:
                continue
            ntl = abs(float(r["pnl_usd"]) / (net / 1e4))
            if ats_col:
                v = (r.get("ats_ratio") or "").strip()
                a = float(v) if v else None
            else:
                mult = ntl / base
                a = mult * SIZE_REF if SIZE_MIN + 1e-6 < mult < SIZE_MAX - 1e-6 else None
            if a is None:
                continue
            out.append(dict(sym=r["symbol"], net=net, ats=a, reason=r["reason"],
                            t=datetime.strptime(r["entry_time"], "%Y-%m-%d %H:%M:%S")))
        except Exception:
            pass
    return sorted(out, key=lambda x: x["ats"])


def pct(v, q):
    s = sorted(v)
    if not s:
        return float("nan")
    i = q * (len(s) - 1)
    lo = int(math.floor(i))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (i - lo)


def block(label, rows):
    n = len(rows)
    if n < 40:
        print(f"\n### {label}: only {n} trades — skipping\n")
        return
    print(f"\n### {label} — {n} trades")
    k = n // 5
    print(f"  {'ats quintile':>16} {'n':>4} {'mean':>8} {'median':>8} {'p10':>9} "
          f"{'worst':>9} {'<-400bps':>9} {'backstop':>9}")
    for i in range(5):
        seg = rows[i*k:(i+1)*k if i < 4 else n]
        b = [r["net"] for r in seg]
        lo, hi = seg[0]["ats"], seg[-1]["ats"]
        nb = sum(1 for r in seg if r["reason"].startswith("backstop"))
        print(f"  {f'Q{i+1} {lo:.1f}-{hi:.1f}':>16} {len(seg):>4} {sum(b)/len(b):>+8.1f} "
              f"{pct(b,0.5):>+8.1f} {pct(b,0.10):>+9.1f} {min(b):>+9.1f} "
              f"{100*sum(1 for x in b if x < -400)/len(b):>8.0f}% "
              f"{100*nb/len(seg):>8.0f}%")

    half = n // 2
    lowa, higha = rows[:half], rows[half:]
    print(f"\n  bottom half of ats vs top half (split at {rows[half]['ats']:.2f}):")
    for lab, seg in (("low  ats", lowa), ("high ats", higha)):
        b = [r["net"] for r in seg]
        mu = sum(b) / len(b)
        sd = math.sqrt(sum((x-mu)**2 for x in b)/(len(b)-1))
        dn = [x for x in b if x < 0]
        print(f"    {lab}  n={len(seg):<4} mean {mu:>+7.1f}  sd {sd:>6.0f}  "
              f"p10 {pct(b,0.10):>+8.1f}  loss rate {100*len(dn)/len(b):>3.0f}%  "
              f"mean loss {sum(dn)/max(1,len(dn)):>+8.1f}  blowups(<-400) {sum(1 for x in b if x<-400)}")
    bl, bh = [r["net"] for r in lowa], [r["net"] for r in higha]
    # is the DOWNSIDE different, controlling for the mean? compare left tails
    print(f"\n    left-tail gap  p10: {pct(bh,0.10)-pct(bl,0.10):+.1f} bps "
          f"| p05: {pct(bh,0.05)-pct(bl,0.05):+.1f} bps "
          f"| mean gap: {sum(bh)/len(bh)-sum(bl)/len(bl):+.1f} bps")
    print("    If the tail gap is much larger than the mean gap, the rule is buying")
    print("    downside protection rather than upside selection.")

    # winners vs losers, separately: which side does ats actually sort?
    print("\n  does ats sort WINNERS, or does it sort LOSERS?")
    for lab, f in (("among winners (net>0)", lambda x: x > 0),
                   ("among losers  (net<0)", lambda x: x < 0)):
        seg = [r for r in rows if f(r["net"])]
        if len(seg) < 20:
            continue
        h = len(seg) // 2
        a = sum(r["net"] for r in seg[:h]) / h
        b = sum(r["net"] for r in seg[h:]) / (len(seg) - h)
        print(f"    {lab:<24} low-ats {a:>+8.1f}  high-ats {b:>+8.1f}  gap {b-a:>+8.1f}")


live = load(LIVE, 35.0, True)
paper = load(PAPER, 100.0, False)
block("LIVE (flat $35 — ats cannot have influenced any outcome)", live)
block("PAPER 15m-ats", paper)
print("\n" + "=" * 78)
print("The strategy has no stop-loss, so its risk is entirely the left tail. A sizing")
print("rule that reliably shrinks the blowups is worth more than its effect on the mean,")
print("and is estimable with far less data.")
