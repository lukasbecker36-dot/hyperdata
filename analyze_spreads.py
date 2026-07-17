import pandas as pd, numpy as np
s=pd.read_csv('spreads_snapshot.csv')
uni=pd.read_csv('perp_universe.csv').set_index('name')['day_notional_vol']
qs=uni.quantile([1/3,2/3]).values
tier=lambda v:'LOW' if v<qs[0] else ('MID' if v<qs[1] else 'HIGH')
s['tier']=s['symbol'].map(lambda n:tier(uni.get(n,0)))
FEE_TAKER=9.0   # bps round-trip (4.5 x2)
FEE_MAKER=3.0   # bps round-trip (1.5 x2)

print("LIVE L2 SNAPSHOT (2026-07-17 18:25 UTC) — top-of-book spread & slippage by liquidity tier\n")
print(f"{'tier':5s} {'n':>3s} | {'spread bps (median/mean/p90)':>30s} | {'top depth $ (med)':>16s} | {'slip 10k$':>9s} {'slip 50k$':>9s}")
for t in ['HIGH','MID','LOW']:
    g=s[s.tier==t]
    sp=g['spread_bps']
    print(f"{t:5s} {len(g):3d} | {sp.median():8.1f} / {sp.mean():6.1f} / {sp.quantile(.9):6.1f}          | {g['top_depth_usd'].median():14,.0f} | {g['slip10k_bps'].median():8.1f} {g['slip50k_bps'].median():8.1f}")

print("\n=== IMPLIED ROUND-TRIP COST by tier (small ~100$ clip: spread dominates, slippage ~0) ===")
print(f"{'tier':5s} | {'TAKER: 9bps fee + full spread':>32s} | {'MAKER: 3bps fee, no spread cross':>34s}")
for t in ['HIGH','MID']:
    g=s[s.tier==t]; med_sp=g['spread_bps'].median()
    taker=FEE_TAKER+med_sp   # cross half-spread in + half-spread out = full spread round trip
    print(f"{t:5s} | {taker:6.1f} bps  (9 + {med_sp:.1f})              | {FEE_MAKER:6.1f} bps  (post both sides)")

print("\n=== vs my backtest assumptions ===")
print("  backtest used: 11bps (taker) round-trip")
hi=s[s.tier=='HIGH']['spread_bps'].median(); mid=s[s.tier=='MID']['spread_bps'].median()
print(f"  reality (calm snapshot): HIGH taker ~ {9+hi:.0f}bps,  MID taker ~ {9+mid:.0f}bps")
print("  NOTE: this is a CALM snapshot; spreads widen materially during the volume spikes we trade on.")

# a few concrete examples across tiers
print("\nexample names (spread bps, top depth $):")
for t in ['HIGH','MID','LOW']:
    g=s[s.tier==t]
    ex=pd.concat([g.nsmallest(2,'spread_bps'),g.nlargest(2,'spread_bps')])
    for _,r in ex.iterrows():
        print(f"  [{t:4s}] {r['symbol']:10s} spread={r['spread_bps']:5.1f}bps  slip10k={r['slip10k_bps']:5.1f}bps")
