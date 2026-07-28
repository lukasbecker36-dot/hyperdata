#!/usr/bin/env python3
"""Hidden-Markov regime study for the volume-exhaustion fade.

Fit a Gaussian HMM on a market-level observation series [BTC hourly return, log market-vol index]
to infer latent regimes, then bucket the strategy's ACTUAL signals (from wide_stop) by the regime
that was knowable AT ENTRY. Two disciplines, because regime conditioning in this repo has been a
single-month artifact before (see commit 946914b):
  1. CAUSAL labels: fit the HMM on the first half only, then label every bar with the FILTERED
     (forward-only) posterior — no future data, no smoothing. Report the 2nd-half OOS regime P&L.
  2. ROBUSTNESS: month-by-month breakdown of the best-vs-worst regime gap (the test that retracted
     the last regime filter), plus a holdout.
Descriptive full-sample (Viterbi) labels are shown too but flagged as in-sample (mild lookahead).
Pure stdlib (no numpy/hmmlearn on this box). 1h panel. Run from analysis/.
"""
import math, sys, os
from collections import defaultdict
_o = sys.stdout; sys.stdout = open(os.devnull, "w")
import wide_stop as w
sys.stdout.close(); sys.stdout = _o

per = w.per_sym; signals = w.signals; COST = w.COST; MAXH = w.MAXH
liquid = [s for s in per if w.tier(w.uni.get(s, 0)) in ('HIGH', 'MID')]
NEG_INF = float('-inf')

# ---------- market observation series on the BTC grid ----------
bt = per['BTC'][0]
btc_ret = {bt[i]: per['BTC'][5][i] for i in range(1, len(bt))}
# trailing 24-bar realized vol per symbol, then cross-sectional median per timestamp
volsum = defaultdict(list)
for s in liquid:
    t, hi, lo, c, v, ret = per[s]
    for i in range(25, len(t)):
        seg = ret[i-24:i]
        m = sum(seg)/24.0; sd = (sum((x-m)**2 for x in seg)/24.0)**0.5
        volsum[t[i]].append(sd)
grid = [ms for ms in bt if ms in btc_ret and ms in volsum and len(volsum[ms]) >= 10]
grid.sort()
def median(xs):
    xs = sorted(xs); n = len(xs); return xs[n//2] if n % 2 else 0.5*(xs[n//2-1]+xs[n//2])
obs = []   # [btc_ret, log(median rv24)]
for ms in grid:
    obs.append((btc_ret[ms], math.log(median(volsum[ms]) + 1e-9)))
T = len(obs)

def standardize(rows, lo, hi):
    """z-score each column using stats from rows[lo:hi] only (causal-safe)."""
    D = len(rows[0]); mu = [0.0]*D; sd = [0.0]*D; n = hi-lo
    for d in range(D):
        col = [rows[k][d] for k in range(lo, hi)]
        mu[d] = sum(col)/n; sd[d] = (sum((x-mu[d])**2 for x in col)/n)**0.5 or 1.0
    return [tuple((r[d]-mu[d])/sd[d] for d in range(D)) for r in rows]

# ---------- Gaussian HMM (diagonal covariance), pure stdlib ----------
def logsumexp(a):
    m = max(a)
    if m == NEG_INF: return NEG_INF
    return m + math.log(sum(math.exp(x-m) for x in a))

def emis_ll(o, mu, var):
    s = 0.0
    for d in range(len(o)):
        s += -0.5*math.log(2*math.pi*var[d]) - (o[d]-mu[d])**2/(2*var[d])
    return s

def fit_hmm(data, K, iters=40, seed=0):
    T = len(data); D = len(data[0])
    # init: sort by 2nd feature (vol) into K quantile groups
    order = sorted(range(T), key=lambda k: data[k][1])
    grp = [0]*T
    for rank, k in enumerate(order): grp[k] = min(K-1, rank*K//T)
    mu = [[0.0]*D for _ in range(K)]; var = [[0.0]*D for _ in range(K)]
    for j in range(K):
        idx = [k for k in range(T) if grp[k] == j] or [0]
        for d in range(D):
            col = [data[k][d] for k in idx]; m = sum(col)/len(col)
            mu[j][d] = m; var[j][d] = max(1e-4, sum((x-m)**2 for x in col)/len(col))
    logpi = [math.log(1.0/K)]*K
    logA = [[math.log((0.90 if a == b else 0.10/(K-1))) for b in range(K)] for a in range(K)]
    prev = None
    for _ in range(iters):
        B = [[emis_ll(data[t], mu[j], var[j]) for j in range(K)] for t in range(T)]
        la = [[NEG_INF]*K for _ in range(T)]
        for j in range(K): la[0][j] = logpi[j] + B[0][j]
        for t in range(1, T):
            for j in range(K):
                la[t][j] = logsumexp([la[t-1][i] + logA[i][j] for i in range(K)]) + B[t][j]
        ll = logsumexp(la[T-1])
        lb = [[NEG_INF]*K for _ in range(T)]
        for j in range(K): lb[T-1][j] = 0.0
        for t in range(T-2, -1, -1):
            for i in range(K):
                lb[t][i] = logsumexp([logA[i][j] + B[t+1][j] + lb[t+1][j] for j in range(K)])
        # gamma, xi
        g = [[la[t][j]+lb[t][j]-ll for j in range(K)] for t in range(T)]
        g = [[math.exp(x) for x in row] for row in g]
        logpi = [math.log(max(g[0][j], 1e-12)) for j in range(K)]
        # transitions
        num = [[NEG_INF]*K for _ in range(K)]
        for t in range(T-1):
            for i in range(K):
                for j in range(K):
                    v = la[t][i] + logA[i][j] + B[t+1][j] + lb[t+1][j] - ll
                    num[i][j] = v if num[i][j] == NEG_INF else logsumexp([num[i][j], v])
        for i in range(K):
            den = logsumexp([num[i][j] for j in range(K)])
            for j in range(K): logA[i][j] = num[i][j] - den
        # emissions
        for j in range(K):
            wsum = sum(g[t][j] for t in range(T)) or 1e-12
            for d in range(D):
                m = sum(g[t][j]*data[t][d] for t in range(T))/wsum
                mu[j][d] = m
                var[j][d] = max(1e-4, sum(g[t][j]*(data[t][d]-m)**2 for t in range(T))/wsum)
        if prev is not None and abs(ll-prev) < 1e-3: break
        prev = ll
    return dict(mu=mu, var=var, logpi=logpi, logA=logA, K=K)

def viterbi(data, H):
    K = H['K']; T = len(data); mu, var, logpi, logA = H['mu'], H['var'], H['logpi'], H['logA']
    B = [[emis_ll(data[t], mu[j], var[j]) for j in range(K)] for t in range(T)]
    d = [[NEG_INF]*K for _ in range(T)]; bp = [[0]*K for _ in range(T)]
    for j in range(K): d[0][j] = logpi[j] + B[0][j]
    for t in range(1, T):
        for j in range(K):
            best = max(range(K), key=lambda i: d[t-1][i]+logA[i][j])
            d[t][j] = d[t-1][best]+logA[best][j]+B[t][j]; bp[t][j] = best
    path = [max(range(K), key=lambda j: d[T-1][j])]
    for t in range(T-1, 0, -1): path.append(bp[t][path[-1]])
    return path[::-1]

def filtered(data, H):
    """Causal forward-only posterior: state label at t from o_1..t only (no future)."""
    K = H['K']; mu, var, logpi, logA = H['mu'], H['var'], H['logpi'], H['logA']
    lab = []; la = [logpi[j] + emis_ll(data[0], mu[j], var[j]) for j in range(K)]
    lab.append(max(range(K), key=lambda j: la[j]))
    for t in range(1, len(data)):
        la = [logsumexp([la[i]+logA[i][j] for i in range(K)]) + emis_ll(data[t], mu[j], var[j])
              for j in range(K)]
        z = logsumexp(la); la = [x-z for x in la]
        lab.append(max(range(K), key=lambda j: la[j]))
    return lab

def relabel_by_vol(H):
    """map raw state index -> rank by emission vol-mean (feature 1), 0=calmest."""
    order = sorted(range(H['K']), key=lambda j: H['mu'][j][1])
    return {raw: rank for rank, raw in enumerate(order)}

# ---------- per-trade P&L tagged with entry timestamp (wide_stop baseline: no stop, hold to backstop) ----------
trades = []   # (entry_ms, net_ret, month)
from datetime import datetime, timezone
for sym, i, brk in signals:
    t, hi, lo, c, v, ret = per[sym]
    if i+MAXH >= len(c): continue
    d = -brk; e = c[i]
    r = d*math.log(c[i+MAXH]/e) - COST
    ms = t[i]; mo = datetime.fromtimestamp(ms/1000, timezone.utc).strftime("%Y-%m")
    trades.append((ms, r, mo))

def bucket_by(labelmap_ms, name_of, tag):
    grp = defaultdict(list)
    for ms, r, mo in trades:
        lab = labelmap_ms.get(ms)
        if lab is None: continue
        grp[lab].append(r)
    print(f"  {tag}:")
    for lab in sorted(grp):
        rs = grp[lab]; n = len(rs); m = sum(rs)/n
        sd = (sum((x-m)**2 for x in rs)/n)**0.5 if n > 1 else 0
        win = sum(1 for x in rs if x > 0)/n*100
        tstat = m/sd*math.sqrt(n) if sd > 0 else 0
        print(f"    {name_of(lab):10s} n={n:5d}  net={m*1e4:+7.1f}bps  win={win:4.1f}%  t={tstat:+5.2f}  cum={sum(rs)*100:+8.1f}%")
    return grp

NAMES = {2: {0: 'CALM', 1: 'STRESS'}, 3: {0: 'LOW-vol', 1: 'MID-vol', 2: 'HIGH-vol'}}

for K in (2, 3):
    print(f"\n{'='*78}\nGaussian HMM, K={K} states  (obs = [BTC ret, log market-vol index], T={T} bars)")
    # ---- full-sample (descriptive, in-sample Viterbi) ----
    dat_full = standardize(obs, 0, T)
    Hf = fit_hmm(dat_full, K)
    rmap = relabel_by_vol(Hf); nm = lambda lab: NAMES[K][lab]
    vit = viterbi(dat_full, Hf)
    lab_ms_full = {grid[t]: rmap[vit[t]] for t in range(T)}
    # regime persistence + emission summary
    dwell = defaultdict(int); cnt = defaultdict(int)
    for t in range(T): cnt[rmap[vit[t]]] += 1
    print(f"  state occupancy: " + "  ".join(f"{nm(lab)}={cnt[lab]/T*100:.0f}%" for lab in sorted(cnt)))
    bucket_by(lab_ms_full, nm, "FULL-SAMPLE Viterbi (in-sample, mild lookahead)")

    # ---- causal: fit on first half, filtered-label whole series, report 2nd-half OOS ----
    half = T//2
    dat_causal = standardize(obs, 0, half)                # z-score using in-sample stats only
    Hc = fit_hmm(dat_causal[:half], K)
    rmapc = relabel_by_vol(Hc)
    fl = filtered(dat_causal, Hc)                          # forward-only over full series
    lab_ms_oos = {grid[t]: rmapc[fl[t]] for t in range(half, T)}   # only 2nd half
    tsplit = grid[half]
    n_oos = sum(1 for ms, r, mo in trades if ms >= tsplit)
    print(f"  --- CAUSAL (fit on 1st half, filtered labels, OOS = 2nd half, {n_oos} trades) ---")
    goos = bucket_by(lab_ms_oos, nm, "OOS filtered (forward-only, no lookahead)")

    # ---- robustness: monthly breakdown of HIGH-vs-CALM gap on the CAUSAL labels ----
    if K == 3:
        hi_lab, lo_lab = 2, 0
    else:
        hi_lab, lo_lab = 1, 0
    lab_ms_all = {grid[t]: rmapc[fl[t]] for t in range(T)}  # reuse the single filtered pass above (causal params)
    permo = defaultdict(lambda: defaultdict(list))
    for ms, r, mo in trades:
        lab = lab_ms_all.get(ms)
        if lab in (hi_lab, lo_lab): permo[mo][lab].append(r)
    print(f"  --- MONTHLY robustness of {nm(hi_lab)} vs {nm(lo_lab)} (causal filtered labels) ---")
    print(f"    {'month':8s} {nm(hi_lab)[:7]:>8s}n  net    {nm(lo_lab)[:7]:>8s}n  net     gap(bps)")
    tot_gap = 0.0
    for mo in sorted(permo):
        hs = permo[mo][hi_lab]; ls = permo[mo][lo_lab]
        hn = len(hs); ln = len(ls)
        hm = sum(hs)/hn*1e4 if hn else float('nan'); lm = sum(ls)/ln*1e4 if ln else float('nan')
        gap = (hm-lm) if (hn and ln) else float('nan')
        if hn and ln: tot_gap += (hm-lm)
        print(f"    {mo:8s} {hn:8d} {hm:+7.1f}   {ln:8d} {lm:+7.1f}   {gap:+8.1f}")
    print(f"    (sum of monthly gaps where both regimes present: {tot_gap:+.1f} bps)")
