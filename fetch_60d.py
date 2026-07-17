import urllib.request, json, time, csv

names=['KAITO-USDC','ENS-USDC','RESOLV-USDC','BANANA-USDC','LDO-USDC','USUAL-USDC','CFX-USDC','APEX-USDC','BSV-USDC','DYDX-USDC']
now=int(time.time()*1000)
start_all=now-60*24*60*60*1000
MIN=60*1000
STEP=4800*MIN   # ~3.33 days per request, under the ~5000 candle cap

def fetch(coin, s, e):
    body=json.dumps({'type':'candleSnapshot','req':{'coin':coin,'interval':'1m','startTime':s,'endTime':e}}).encode()
    req=urllib.request.Request('https://api.hyperliquid.xyz/info',data=body,headers={'Content-Type':'application/json'})
    for attempt in range(4):
        try:
            return json.load(urllib.request.urlopen(req,timeout=60))
        except Exception as ex:
            if attempt==3: raise
            time.sleep(1.5*(attempt+1))

rows=[]
for n in names:
    coin=n.replace('-USDC','')
    seen={}
    s=start_all
    while s<now:
        e=min(s+STEP, now)
        data=fetch(coin,s,e)
        for c in data:
            seen[c['t']]=c
        s=e
        time.sleep(0.15)
    for t in sorted(seen):
        c=seen[t]
        rows.append([n,c['t'],c['T'],
            time.strftime('%Y-%m-%d %H:%M:%S',time.gmtime(c['t']/1000)),
            c['o'],c['h'],c['l'],c['c'],c['v'],c['n']])
    print(n, len(seen), 'candles', flush=True)

with open('hyperliquid_1m_60d.csv','w',newline='') as f:
    w=csv.writer(f)
    w.writerow(['symbol','open_time_ms','close_time_ms','open_time_utc','open','high','low','close','volume','num_trades'])
    w.writerows(rows)
print('TOTAL rows:',len(rows))
