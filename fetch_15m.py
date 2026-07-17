import urllib.request, json, time, csv
names=['KAITO-USDC','ENS-USDC','RESOLV-USDC','BANANA-USDC','LDO-USDC','USUAL-USDC','CFX-USDC','APEX-USDC','BSV-USDC','DYDX-USDC']
now=int(time.time()*1000); start=now-60*24*60*60*1000
def fetch(coin):
    body=json.dumps({'type':'candleSnapshot','req':{'coin':coin,'interval':'15m','startTime':start,'endTime':now}}).encode()
    req=urllib.request.Request('https://api.hyperliquid.xyz/info',data=body,headers={'Content-Type':'application/json'})
    for a in range(4):
        try: return json.load(urllib.request.urlopen(req,timeout=60))
        except Exception:
            if a==3: raise
            time.sleep(1.5*(a+1))
rows=[]
for n in names:
    d=fetch(n.replace('-USDC',''))
    for c in d:
        rows.append([n,c['t'],c['T'],time.strftime('%Y-%m-%d %H:%M:%S',time.gmtime(c['t']/1000)),
            c['o'],c['h'],c['l'],c['c'],c['v'],c['n']])
    print(n,len(d),'candles',time.strftime('%Y-%m-%d',time.gmtime(d[0]['t']/1000)),'->',time.strftime('%Y-%m-%d',time.gmtime(d[-1]['t']/1000)),flush=True)
    time.sleep(0.2)
with open('hyperliquid_15m_60d.csv','w',newline='') as f:
    w=csv.writer(f); w.writerow(['symbol','open_time_ms','close_time_ms','open_time_utc','open','high','low','close','volume','num_trades']); w.writerows(rows)
print('TOTAL',len(rows))
