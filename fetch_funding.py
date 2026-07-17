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
        # fundingHistory may cap; paginate forward
        s=start; seen={}
        while s<now:
            d=post({'type':'fundingHistory','coin':name,'startTime':s,'endTime':now})
            if not d: break
            for e in d: seen[e['time']]=e['fundingRate']
            last=d[-1]['time']
            if last<=s: break
            s=last+1
            time.sleep(0.05)
        for t in sorted(seen):
            rows.append([name,t,seen[t]])
    except Exception as e:
        print('ERR',name,e); continue
    done+=1
    if done%40==0: print(f'{done}/{len(names)} rows={len(rows)}',flush=True)
    time.sleep(0.05)
with open('hyperliquid_funding.csv','w',newline='') as f:
    w=csv.writer(f); w.writerow(['symbol','time_ms','funding_rate']); w.writerows(rows)
print('TOTAL symbols=%d rows=%d'%(done,len(rows)))
