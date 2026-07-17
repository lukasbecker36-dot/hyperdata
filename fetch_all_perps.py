import urllib.request, json, time, csv
def post(body):
    req=urllib.request.Request('https://api.hyperliquid.xyz/info',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
    for a in range(5):
        try: return json.load(urllib.request.urlopen(req,timeout=60))
        except Exception:
            if a==4: raise
            time.sleep(1.5*(a+1))

m=post({'type':'metaAndAssetCtxs'}); uni=m[0]['universe']; ctxs=m[1]
active=[(u['name'], float(c['dayNtlVlm'])) for u,c in zip(uni,ctxs) if c.get('midPx') is not None]
print('active core perps:',len(active))

# save the universe + 24h volume for liquidity segmentation later
with open('perp_universe.csv','w',newline='') as f:
    w=csv.writer(f); w.writerow(['name','day_notional_vol']); w.writerows(active)

now=int(time.time()*1000); start=now-60*24*60*60*1000
rows=[]; done=0
for name,_ in active:
    try:
        d=post({'type':'candleSnapshot','req':{'coin':name,'interval':'15m','startTime':start,'endTime':now}})
    except Exception as e:
        print('  ERR',name,e); continue
    for c in d:
        rows.append([name,c['t'],c['o'],c['h'],c['l'],c['c'],c['v'],c['n']])
    done+=1
    if done%25==0: print(f'  {done}/{len(active)} fetched, rows={len(rows)}',flush=True)
    time.sleep(0.12)

with open('hyperliquid_15m_allperps.csv','w',newline='') as f:
    w=csv.writer(f); w.writerow(['symbol','open_time_ms','open','high','low','close','volume','num_trades']); w.writerows(rows)
print('TOTAL symbols=%d rows=%d'%(done,len(rows)))
