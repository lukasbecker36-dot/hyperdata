import urllib.request, json, time, csv
def post(b):
    r=urllib.request.Request('https://api.hyperliquid.xyz/info',data=json.dumps(b).encode(),headers={'Content-Type':'application/json'})
    for a in range(4):
        try: return json.load(urllib.request.urlopen(r,timeout=30))
        except Exception:
            if a==3: raise
            time.sleep(1.0*(a+1))

names=[r.split(',')[0] for r in open('perp_universe.csv').read().splitlines()[1:]]
SIZES=[1000,10000,50000]   # USD notional to walk the book for slippage
rows=[]
for nm in names:
    try:
        b=post({'type':'l2Book','coin':nm})
    except Exception as e:
        print('ERR',nm,e); continue
    lv=b.get('levels');
    if not lv or len(lv)<2 or not lv[0] or not lv[1]: continue
    bids=lv[0]; asks=lv[1]
    bid=float(bids[0]['px']); ask=float(asks[0]['px']); mid=(bid+ask)/2
    spread_bps=(ask-bid)/mid*1e4
    topdepth=min(float(bids[0]['px'])*float(bids[0]['sz']), float(asks[0]['px'])*float(asks[0]['sz']))
    # slippage: VWAP to BUY X notional off asks, vs mid, in bps
    slip={}
    for X in SIZES:
        need=X; cost=0.0; filled=0.0; ok=False
        for a in asks:
            px=float(a['px']); szn=px*float(a['sz'])
            take=min(need,szn); cost+=take/px*px  # notional
            filled+=take/px; need-=take
            if need<=0: ok=True; break
        if ok and filled>0:
            vwap=X/filled; slip[X]=(vwap-mid)/mid*1e4
        else:
            slip[X]=None   # book too thin to fill X
    rows.append([nm,bid,ask,mid,spread_bps,topdepth,slip[1000],slip[10000],slip[50000]])
    time.sleep(0.05)

with open('spreads_snapshot.csv','w',newline='') as f:
    w=csv.writer(f); w.writerow(['symbol','bid','ask','mid','spread_bps','top_depth_usd','slip1k_bps','slip10k_bps','slip50k_bps']); w.writerows(rows)
print('snapshot symbols:',len(rows),'time:',time.strftime('%Y-%m-%d %H:%M UTC',time.gmtime()))
