import urllib.request, json, time, csv
def post(b):
    r=urllib.request.Request('https://api.hyperliquid.xyz/info',data=json.dumps(b).encode(),headers={'Content-Type':'application/json'})
    for a in range(5):
        try: return json.load(urllib.request.urlopen(r,timeout=60))
        except Exception:
            if a==4: raise
            time.sleep(1.5*(a+1))
names=[r.split(',')[0] for r in open('perp_universe.csv').read().splitlines()[1:]]
now=int(time.time()*1000); start=now-20*24*60*60*1000
rows=[]; done=0
for nm in names:
    try: d=post({'type':'candleSnapshot','req':{'coin':nm,'interval':'5m','startTime':start,'endTime':now}})
    except Exception as e: print('ERR',nm,e); continue
    for c in d: rows.append([nm,c['t'],c['o'],c['h'],c['l'],c['c'],c['v'],c['n']])
    done+=1
    if done%40==0: print(f'{done}/{len(names)} rows={len(rows)}',flush=True)
    time.sleep(0.1)
with open('hyperliquid_5m.csv','w',newline='') as f:
    w=csv.writer(f); w.writerow(['symbol','open_time_ms','open','high','low','close','volume','num_trades']); w.writerows(rows)
print('TOTAL symbols=%d rows=%d'%(done,len(rows)))
