#!/usr/bin/env python3
"""
Weekend gap logger -- record (never trade) the price gap between the same stock / commodity / index
perp on Hyperliquid's xyz dex and on Aster, every minute, to see whether it opens up while the
underlying market is shut and closes again when it reopens.

WHY. Both venues keep these perps trading 24/7, but while the underlying is closed each one prices
off its own book and oracle. A Sunday-evening snapshot (1 Nov 2026) had Aster 0.1-0.3% above
Hyperliquid on nearly every matched name (GOLD +15bp, NVDA +26bp, GOOGL +28bp, MU +20bp). If such
gaps are regularly wider than both venues' costs and shut by the Monday open, buying the cheap side
and shorting the rich side is close to market-neutral. Funding is NOT the edge: both charge about the
same carry (gold ~+7%/yr, Brent ~-30%/yr on each), so the cross-venue funding spread is 1-3%/yr.

WHAT IS LOGGED, once a minute (one Hyperliquid and two Aster requests, so it costs nothing):
  Hyperliquid: metaAndAssetCtxs(dex=xyz) -> mid, impact bid/ask (the price to trade the venue's
    impact notional), mark, oracle, hourly funding, 24h volume
  Aster: ticker/bookTicker (best bid/ask + size) and premiumIndex (mark, index, funding)
Pairs are names listed on both (xyz:GOLD = XAUUSDT, SILVER = XAG, BRENTOIL = BZ, the rest by ticker)
whose marks agree within 2% when the logger starts, so different instruments under the same ticker
(Aster's SPX is a meme coin) drop out. The Nasdaq-100 / S&P pairs are kept as ratio pairs
(xyz:XYZ100 vs QQQ, xyz:SP500 vs SPY): their gap is measured against the pair's own median ratio.

GAP (bp of mid), executable at the quoted tops, before fees:
  sell Aster / buy HL = (aster_bid - hl_ask) / mid      buy Aster / sell HL = (hl_bid - aster_ask) / mid
  gap = the larger of the two (negative = no crossing trade even before costs).

OUTPUT (datadir): gap_YYYYMMDD.csv (gzipped once the day is over), service.out. /gap in @Hyperfadebot
shows the last 7 days by session; a summary goes to Telegram on Mondays at 14:00 UTC, before the US open.

  python3 weekend_gap.py --datadir ./gap            # run
  python3 weekend_gap.py --datadir ./gap --once     # one sample, print the widest gaps, exit
  python3 weekend_gap.py --datadir ./gap --report   # print the 7-day report
"""
import argparse
import csv
import gzip
import json
import math
import statistics
import time
import urllib.request
from pathlib import Path

import telegram_notify as tg

HL_INFO = "https://api.hyperliquid.xyz/info"
ASTER = "https://fapi.asterdex.com/fapi/v1/"
ALIAS = {"GOLD": "XAUUSDT", "SILVER": "XAGUSDT", "BRENTOIL": "BZUSDT"}
RATIO_PAIRS = {"XYZ100": "QQQUSDT", "SP500": "SPYUSDT"}      # different units: gap vs the pair's median ratio
MATCH_TOL = 0.02          # marks within 2% at start = same instrument
COLS = ["ts_ms", "pair", "aster", "ratio", "hl_mid", "hl_bid", "hl_ask", "hl_mark", "hl_oracle", "hl_fund_h",
        "hl_vlm", "as_bid", "as_ask", "as_bid_qty", "as_ask_qty", "as_mark", "as_index", "as_fund"]
THRESH_BP = (10, 20, 40)


def _get(url, body=None, timeout=15):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "weekend-gap"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch():
    """One snapshot: (hl {name: ctx}, aster book {sym: row}, aster premium {sym: row})."""
    meta, ctxs = _get(HL_INFO, {"type": "metaAndAssetCtxs", "dex": "xyz"})
    hl = {u["name"].split(":", 1)[-1]: c for u, c in zip(meta["universe"], ctxs)}
    book = {x["symbol"]: x for x in _get(ASTER + "ticker/bookTicker")}
    prem = {x["symbol"]: x for x in _get(ASTER + "premiumIndex")}
    return hl, book, prem


def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def pairs(hl, prem):
    """name -> (aster symbol, is_ratio) for instruments listed on both venues."""
    out = {}
    for name, c in hl.items():
        if name in RATIO_PAIRS:
            if RATIO_PAIRS[name] in prem:
                out[name] = (RATIO_PAIRS[name], True)
            continue
        sym = ALIAS.get(name, name + "USDT")
        hm, am = _f(c.get("markPx")), _f((prem.get(sym) or {}).get("markPrice"))
        if hm and am and abs(am / hm - 1) <= MATCH_TOL:
            out[name] = (sym, False)
    return out


def rows_for(ts_ms, pmap, hl, book, prem):
    out = []
    for name, (sym, ratio) in sorted(pmap.items()):
        c, b, p = hl.get(name), book.get(sym), prem.get(sym)
        if not (c and b and p):
            continue
        imp = c.get("impactPxs") or [None, None]
        out.append({"ts_ms": ts_ms, "pair": name, "aster": sym, "ratio": int(ratio),
                    "hl_mid": c.get("midPx"), "hl_bid": imp[0], "hl_ask": imp[1], "hl_mark": c.get("markPx"),
                    "hl_oracle": c.get("oraclePx"), "hl_fund_h": c.get("funding"), "hl_vlm": c.get("dayNtlVlm"),
                    "as_bid": b.get("bidPrice"), "as_ask": b.get("askPrice"), "as_bid_qty": b.get("bidQty"),
                    "as_ask_qty": b.get("askQty"), "as_mark": p.get("markPrice"), "as_index": p.get("indexPrice"),
                    "as_fund": p.get("lastFundingRate")})
    return out


def gap_bp(r, ratio=1.0):
    """(best executable gap, direction) in bp of mid; Aster prices divided by `ratio` first.
    direction "A>H" = sell Aster / buy Hyperliquid."""
    hb, ha = _f(r.get("hl_bid")), _f(r.get("hl_ask"))
    ab, aa = _f(r.get("as_bid")), _f(r.get("as_ask"))
    if None in (hb, ha, ab, aa) or min(hb, ha, ab, aa) <= 0:
        return None, None
    ab, aa = ab / ratio, aa / ratio
    mid = (hb + ha + ab + aa) / 4
    sell_a, buy_a = (ab - ha) / mid * 1e4, (hb - aa) / mid * 1e4
    return (sell_a, "A>H") if sell_a >= buy_a else (buy_a, "H>A")


def session(ts_ms):
    """'weekend' (Fri 21:00 - Sun 22:00 UTC, US stocks and futures shut), 'US hours' (Mon-Fri 13:30-20:00),
    else 'off-hours' (weekday nights: Asia/Europe, US closed)."""
    t = time.gmtime(ts_ms / 1000)
    mins = t.tm_hour * 60 + t.tm_min
    if (t.tm_wday == 4 and mins >= 21 * 60) or t.tm_wday == 5 or (t.tm_wday == 6 and mins < 22 * 60):
        return "weekend"
    if t.tm_wday < 5 and 13 * 60 + 30 <= mins < 20 * 60:
        return "US hours"
    return "off-hours"


def day_path(datadir, ts_ms):
    return Path(datadir) / f"gap_{time.strftime('%Y%m%d', time.gmtime(ts_ms / 1000))}.csv"


def append(datadir, rows):
    if not rows:
        return
    path = day_path(datadir, rows[0]["ts_ms"])
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        if new:
            w.writeheader()
        w.writerows(rows)


def gzip_old(datadir, now_ms):
    """Compress every finished day's file."""
    today = day_path(datadir, now_ms).name
    for p in Path(datadir).glob("gap_*.csv"):
        if p.name < today:
            with open(p, "rb") as src, gzip.open(str(p) + ".gz", "wb") as dst:
                dst.write(src.read())
            p.unlink()


def load(datadir, now_ms, days=7):
    out = []
    for d in range(days, -1, -1):
        stem = day_path(datadir, now_ms - d * 86_400_000)
        for p in (stem, Path(str(stem) + ".gz")):
            if p.exists():
                with (gzip.open if p.suffix == ".gz" else open)(p, "rt", newline="") as f:
                    out += [r for r in csv.DictReader(f) if now_ms - int(r["ts_ms"]) <= days * 86_400_000]
    return out


def gaps(rows):
    """Rows -> list of (ts_ms, pair, session, gap_bp, direction, hl_vlm). Ratio pairs are measured
    against their median Aster/HL mid ratio over the rows given."""
    med = {}
    for r in rows:
        if str(r["ratio"]) == "1":
            hm, am = _f(r["hl_mid"]), _f(r["as_mark"])
            if hm and am:
                med.setdefault(r["pair"], []).append(am / hm)
    med = {k: statistics.median(v) for k, v in med.items()}
    out = []
    for r in rows:
        ratio = med.get(r["pair"]) if str(r["ratio"]) == "1" else 1.0
        if ratio is None:
            continue
        g, d = gap_bp(r, ratio)
        if g is not None:
            ts = int(r["ts_ms"])
            out.append((ts, r["pair"], session(ts), g, d, _f(r["hl_vlm"]) or 0.0))
    return out


def report(datadir, now_ms=None, days=7, top=8):
    now_ms = now_ms or time.time() * 1000
    G = gaps(load(datadir, now_ms, days))
    if not G:
        return "<b>Weekend gap</b>: no samples yet"
    first = min(g[0] for g in G)
    lines = [f"<b>Weekend gap</b> — HL xyz vs Aster, same perp, executable at the tops, before fees\n"
             f"{len({g[1] for g in G})} pairs · since {time.strftime('%a %d %b %H:%M', time.gmtime(first / 1000))} UTC"]
    for ses in ("weekend", "off-hours", "US hours"):
        x = [g for g in G if g[2] == ses]
        if not x:
            lines.append(f"\n<b>{ses}</b>: no samples")
            continue
        v = sorted(g[3] for g in x)
        lines.append(f"\n<b>{ses}</b> ({len(x):,} pair-minutes): median {statistics.median(v):+.0f}bp, "
                     f"p90 {v[int(0.9 * (len(v) - 1))]:+.0f}bp · "
                     + " · ".join(f"&gt;{t}bp {sum(g > t for g in v) / len(v):.0%}" for t in THRESH_BP))
        by = {}
        for g in x:
            by.setdefault(g[1], []).append(g)
        rank = sorted(by.items(), key=lambda kv: -sum(g[3] > 20 for g in kv[1]) / len(kv[1]))[:top]
        for pair, gs in rank:
            v = [g[3] for g in gs]
            a_rich = sum(g[4] == "A>H" for g in gs) / len(gs)
            lines.append(f"• {pair}: median {statistics.median(v):+.0f}bp, max {max(v):+.0f}bp, &gt;20bp "
                         f"{sum(g > 20 for g in v) / len(v):.0%} of minutes · Aster rich {a_rich:.0%} · "
                         f"HL ${gs[-1][5] / 1e6:,.1f}M/day")
    lines.append("\n<i>Costs to beat: taker both sides in and out ≈ 20-30bp round trip; makers less. "
                 "Next: does a weekend gap shut by the Monday open?</i>")
    return "\n".join(lines)


def run(datadir, once=False):
    datadir = Path(datadir)
    datadir.mkdir(parents=True, exist_ok=True)
    pmap = None
    print(f"weekend gap logger up | {datadir}", flush=True)
    while True:
        t0 = time.time()
        try:
            hl, book, prem = fetch()
            if pmap is None or (len(pmap) < 10):
                pmap = pairs(hl, prem)
                print(f"{len(pmap)} pairs: {', '.join(sorted(pmap))}", flush=True)
            now_ms = int(t0 * 1000)
            rows = rows_for(now_ms, pmap, hl, book, prem)
            append(datadir, rows)
            gzip_old(datadir, now_ms)
            if once:
                G = sorted(gaps(rows), key=lambda g: -g[3])
                for g in G[:15]:
                    print(f"{g[1]:10s} {g[3]:+6.1f}bp {g[4]} {g[2]}")
                return
            t = time.gmtime(t0)
            week, marker = time.strftime("%G-%V", t), datadir / "last_report_week"
            if t.tm_wday == 0 and t.tm_hour == 14 and (marker.read_text() if marker.exists() else "") != week:
                marker.write_text(week)                 # once per week, even across restarts
                tg.send(report(datadir, now_ms, days=3))
        except Exception as e:                       # never die on a bad response
            print(f"sample failed: {e}", flush=True)
            if once:
                raise
        time.sleep(max(1.0, 60 - (time.time() - t0)))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--datadir", default="./gap")
    p.add_argument("--once", action="store_true")
    p.add_argument("--report", action="store_true")
    a = p.parse_args()
    if a.report:
        print(report(a.datadir).replace("&gt;", ">"))
    else:
        run(a.datadir, a.once)


if __name__ == "__main__":
    main()
