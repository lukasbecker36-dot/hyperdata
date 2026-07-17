import urllib.request, json, time, csv
def post(body):
    req=urllib.request.Request('https://api.hyperliquid.xyz/info',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
    for a in range(5):
        try: return json.load(urllib.request.urlopen(req,timeout=60))
        except Exception:
            if a==4: raise
            time.sleep(1.5*(a+1))
names=[r.split(',')[0] for r in open('perp_universe.csv').read().splitlines()[1:]]
now=int(time.time()*1000); start=now-210*24*60*60*1000
rows=[]; done=0
for name in names:
    try:
        d=post({'type':'candleSnapshot','req':{'coin':name,'interval':'1h','startTime':start,'endTime':now}})
    except Exception as e:
        print('ERR',name,e); continue
    for c in d:
        rows.append([name,c['t'],c['o'],c['h'],c['l'],c['c'],c['v'],c['n']])
    done+=1
    if done%40==0: print(f'{done}/{len(names)} rows={len(rows)}',flush=True)
    time.sleep(0.1)
with open('hyperliquid_1h_history.csv','w',newline='') as f:
    w=csv.writer(f); w.writerow(['symbol','open_time_ms','open','high','low','close','volume','num_trades']); w.writerows(rows)
# report coverage
import collections
print('TOTAL symbols=%d rows=%d'%(done,len(rows)))
