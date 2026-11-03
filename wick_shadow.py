#!/usr/bin/env python3
"""
Wick shadow -- paper-trade (never place an order) the micro-wick fade that 0xcb02837c runs on
Hyperliquid, from this server, with this server's real feed lag and order latency.

THE IDEA. Resting fade orders sit 200bp and 300bp beyond the price 5s earlier, both sides, on
coins doing >= $2M/day. A sudden sweep through them (a liquidation, a big market order into a
thin book) usually snaps back; a move that walks there over several seconds usually keeps going.
So the moment any print is >= 80bp beyond the last 5s extreme (the TRIGGER), pull that coin's
ladder. Whatever filled before the cancel landed is kept; exit with a resting order halfway back
to the reference price, else at market 60s after the fill. Ladder back on 120s after the last
trigger print, once flat.

Tape study 21 Jul - 31 Oct 2026 (58k triggers, 180 coins), net of 1.5bp maker in and a maker
(1.5bp) or taker (6.5bp incl. slippage) exit, with the pull landing 0.6s after the trigger:
  +200bp: +21bp/fill (<$2M/day coins), +41 ($2-10M), +45 (>$10M); 70-90% win, ~10-25 fills/day
  +300bp: +67-89bp on $2M+ coins, 1-2 fills/day
  a ladder that is never pulled loses (-8 to -17bp/fill): the edge is entirely in the pull.
Measured from Nuremberg (1 Nov): trades arrive ~0.38s late (p90 0.58s), /info round trip ~0.23s.

REQUEST BUDGET. Hyperliquid's address limit counts every order in a batch, and the account's
allowance is cumulative: 10k + 1 per $1 ever traded (289,591 on 1 Nov, 11,629 used). Replaying
18-31 Aug through this engine (35 coins, 0.4s lag, 0.23s round trip; ref = this engine's):
  re-peg both sides at 10bp drift       68 fills  +63bp  $124   183,000 requests/day
  re-peg the near side at 30bp          66 fills  +61bp  $113    20,700
  re-peg the near side at 60bp (used)   82 fills  +49bp  $115     6,500
  re-peg both sides at 120bp           190 fills  +20bp  $100     3,800  (stale ladders fill on drifts)
All 68 fills of the first run matched a tape-study event within 10s.

LATENCY MODEL. Exchange time = trade timestamps; local time = when we saw them (NTP-synced, so
the two clocks are comparable). An order sent at local time T is live at the exchange from
T + one-way (half the measured /info round trip, re-measured every 30s). A cancel sent at T
stops fills from T + one-way, so prints that happened before that but reached us later still
fill -- that is exactly the sweep we want. Moving the ladder = a modify: the old price is dead
and the new one live at the same instant. A fill needs a print strictly THROUGH the level (the
whole price level, us included, was taken) and its size is capped at SHARE of the $ printed at
or through the level in the following 2s (others queue there too, this wallet among them).

SAFETY PULLS (what the live bot will do): pull everything while feed lag > MAX_LAG_MS, and on any
websocket disconnect (positions then exit at market on the first print after reconnecting).

OUTPUT (datadir): fills.csv (one row per closed paper position), triggers.csv, lag.csv (feed lag
per minute), state.json (order/modify/cancel counts = the request budget the live bot would use).
Daily summary to Telegram (@Hyperfadebot via telegram_notify) at 00:05 UTC; /wick in the monitor.

  python3 wick_shadow.py --datadir ./wick                               # live shadow
  python3 wick_shadow.py --datadir /tmp/wr --replay tape/tape_20261031.csv.gz --lag-ms 400 --rtt-ms 230
"""
import argparse
import csv
import gzip
import http.client
import json
import os
import statistics
import threading
import time
from collections import deque
from pathlib import Path

import telegram_notify as tg

LEVELS_BP = (200, 300)
TRIG_BP = 80             # trigger: a print this far beyond the last 5s high/low
WIN_MS = 5000            # trigger window, and how far back the ladder's reference price is
REPEG_MS = 1500          # move the ladder at most this often...
REPEG_MIN_BP = 60        # ...and a side only once the price has come this much closer to it (2 requests)
REARM_MS = 120_000       # ladder back on this long after the last trigger print, once flat
MAX_HOLD_MS = 60_000
SHARE = 0.20             # our share of the $ printed at/through a level
CAP_WIN_MS = 2000
NOTIONAL = 300.0         # $ per level (planned live test size)
MAKER_BP, TAKER_BP = 1.5, 6.5
MAX_LAG_MS = 1500        # pull everything while the feed is this late
MIN_VLM = 2e6            # coins doing >= $2M/day
MAX_COINS = 60
INF = float("inf")
FILL_COLS = ["trig_ms", "fill_ms", "exit_ms", "coin", "vlm", "level_bp", "side", "ref", "entry", "exit",
             "reason", "tf_ms", "lag_ms", "ow_ms", "through_usd", "size_usd", "net_bp", "pnl_usd"]
TRIG_COLS = ["t_ms", "coin", "dir", "ladder_up", "lag_ms"]
LAG_COLS = ["minute_ms", "n", "p50", "p90", "max", "ow_ms"]
COUNTERS = ("orders", "modifies", "cancels", "lag_pulls", "disconnects", "triggers", "fills")


def vlm_bucket(v):
    if v is None:
        return "?"
    return "$2-10M" if v < 10e6 else ("$10-50M" if v < 50e6 else ">$50M")


def _append(path, cols, row):
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        w.writerow(row)


class Coin:
    def __init__(self, name, vlm=None):
        self.name, self.vlm = name, vlm
        self.win = deque()       # (t, px) over the last WIN_MS
        self.mx = deque()        # monotonic max / min over the same window
        self.mn = deque()
        self.aged = None         # last price at least WIN_MS old: the ladder's reference
        self.last = None
        self.orders = []         # {side, lvl, px, ref, live, dead}
        self.up = False
        self.ref = {}            # side -> reference price that side's levels hang off
        self.last_peg = -INF
        self.trig_t = -INF       # last trigger print (exchange ms)
        self.event_t = -INF      # start of the current trigger event
        self.pos = []


class Engine:
    """All the strategy logic; fed trades by the live websocket loop or by a tape replay."""

    def __init__(self, datadir, vlm=None, ow_ms=120.0, repeg_bp=None):
        self.dir = Path(datadir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.vlm = vlm or {}
        self.coins = {}
        self.ow = ow_ms          # one-way latency to the exchange
        self.repeg_bp = REPEG_MIN_BP if repeg_bp is None else repeg_bp
        self.lag = 0.0
        self.lag_bad = False
        self.connected = True
        self.cnt = dict.fromkeys(COUNTERS, 0)
        self.lag_min = None
        self.lag_buf = []
        self.state_file = self.dir / "state.json"
        try:
            st = json.loads(self.state_file.read_text())
            self.cnt.update({k: st.get(k, 0) for k in COUNTERS})
            self.since = st.get("since")
        except (FileNotFoundError, json.JSONDecodeError):
            self.since = None
        self.last_t = None

    # ---------------- ladder ----------------
    def _place(self, c, now, sides=(1, -1)):
        for side in sides:               # +1 = resting buys below, -1 = resting sells above
            c.ref[side] = c.aged
            for lvl in LEVELS_BP:
                c.orders.append({"side": side, "lvl": lvl, "px": c.aged * (1 - side * lvl / 1e4),
                                 "ref": c.aged, "live": now + self.ow, "dead": INF})
        c.up, c.last_peg = True, now

    def _pull(self, c, now):
        n = 0
        for o in c.orders:
            if o["dead"] == INF:
                o["dead"] = now + self.ow
                n += 1
        c.up = False
        self.cnt["cancels"] += n           # address limit counts every order in a batch
        return n

    def pull_all(self, now):
        for c in self.coins.values():
            self._pull(c, now)

    def _maybe_arm(self, c, t, now):
        if c.aged is None or self.lag_bad or not self.connected or c.pos:
            return
        if not c.up:
            if t - c.trig_t >= REARM_MS:
                self._place(c, now)
                self.cnt["orders"] += 2 * len(LEVELS_BP)
        elif now - c.last_peg >= REPEG_MS:
            # Only the side the price is moving towards has to stay accurate (a stale near side
            # gets filled by slow drifts, which lose); the far side just sits deeper, so it is
            # refreshed only at twice the threshold. Each side moved = one batchModify.
            for side in (1, -1):
                toward = side * (c.ref[side] - c.aged) / c.ref[side] * 1e4
                if toward >= self.repeg_bp or toward <= -2 * self.repeg_bp:
                    for o in c.orders:
                        if o["side"] == side and o["dead"] == INF:
                            o["dead"] = now + self.ow
                    self._place(c, now, (side,))
                    self.cnt["modifies"] += len(LEVELS_BP)

    # ---------------- events ----------------
    def on_lag(self, now, lag):
        self.lag = lag
        m = int(now // 60000) * 60000
        if self.lag_min is not None and m != self.lag_min and self.lag_buf:
            self.flush_lag()
        self.lag_min = m
        self.lag_buf.append(lag)
        if lag > MAX_LAG_MS and not self.lag_bad:
            self.lag_bad = True
            self.cnt["lag_pulls"] += 1
            self.pull_all(now)
        elif self.lag_bad and lag < 0.7 * MAX_LAG_MS:
            self.lag_bad = False

    def flush_lag(self):
        b = sorted(self.lag_buf)
        _append(self.dir / "lag.csv", LAG_COLS, {
            "minute_ms": self.lag_min, "n": len(b), "p50": round(b[len(b) // 2]),
            "p90": round(b[int(len(b) * 0.9)]), "max": round(b[-1]), "ow_ms": round(self.ow)})
        self.lag_buf = []
        self.save_state()

    def on_disconnect(self, now, planned=False):
        self.connected = False
        self.cnt["disconnects"] += not planned
        for c in self.coins.values():
            for o in c.orders:                 # nothing fills while we are blind
                o["dead"] = min(o["dead"], now)
            c.up = False
            # Forget the price window: after the gap (or a coin dropping out of the universe and
            # back in) the first print would otherwise age a stale price into the reference and
            # hang the ladder off it (RUNE 2 Nov: ref 12h old, "filled" 17% above the market).
            c.win.clear()
            c.mx.clear()
            c.mn.clear()
            c.aged = None
            for p in c.pos:
                if p["exit_at"] is None:
                    p["exit_at"], p["reason"] = now, "disconnect"

    def on_connect(self):
        self.connected = True

    def on_trade(self, t, coin, px, sz, now):
        c = self.coins.get(coin)
        if c is None:
            c = self.coins[coin] = Coin(coin, self.vlm.get(coin))
        if self.since is None:
            self.since = t
        self.last_t = t
        c.last = px
        c.win.append((t, px))
        while c.win and c.win[0][0] < t - WIN_MS:
            c.aged = c.win.popleft()[1]
        while c.mx and c.mx[-1][1] <= px:
            c.mx.pop()
        c.mx.append((t, px))
        while c.mx[0][0] < t - WIN_MS:
            c.mx.popleft()
        while c.mn and c.mn[-1][1] >= px:
            c.mn.pop()
        c.mn.append((t, px))
        while c.mn[0][0] < t - WIN_MS:
            c.mn.popleft()

        d = 1 if px >= c.mn[0][1] * (1 + TRIG_BP / 1e4) else (-1 if px <= c.mx[0][1] * (1 - TRIG_BP / 1e4) else 0)
        if d:
            if t - c.trig_t >= REARM_MS:
                c.event_t = t
                self.cnt["triggers"] += 1
                _append(self.dir / "triggers.csv", TRIG_COLS,
                        {"t_ms": t, "coin": coin, "dir": d, "ladder_up": int(c.up), "lag_ms": round(self.lag)})
            c.trig_t = t
            if c.up:
                self._pull(c, now)

        # open positions: capacity, take-profit, pending market exits
        for p in list(c.pos):
            if t <= p["cap_end"] and p["side"] * (p["entry"] - px) >= 0:
                p["through"] += px * sz
            if p["exit_at"] is not None and t >= p["exit_at"]:
                self._close(c, p, px, p["reason"], True, t)
            elif t >= p["tp_live"] and p["side"] * (px - p["tp"]) > 0:
                self._close(c, p, p["tp"], "tp", False, t)

        # fills: a print strictly through a live level
        for o in list(c.orders):
            if o["live"] <= t < o["dead"] and o["side"] * (o["px"] - px) > 0:
                c.orders.remove(o)
                self.cnt["fills"] += 1
                c.pos.append({"side": o["side"], "lvl": o["lvl"], "entry": o["px"], "ref": o["ref"],
                              "fill_t": t, "recv": now, "trig_t": c.event_t if t - c.event_t <= 60_000 else None,
                              "tp": o["ref"] * (1 - o["side"] * o["lvl"] / 2e4), "tp_live": now + self.ow,
                              "cap_end": t + CAP_WIN_MS, "through": px * sz, "exit_at": None, "reason": None,
                              "lag": self.lag, "ow": self.ow})
        if any(p["fill_t"] == t for p in c.pos):
            self._pull(c, now)                  # whatever is left of the ladder comes off after a fill
        c.orders = [o for o in c.orders if o["dead"] > t]
        self._maybe_arm(c, t, now)

    def on_tick(self, now):
        for c in self.coins.values():
            for p in c.pos:
                if p["exit_at"] is None and now >= p["fill_t"] + MAX_HOLD_MS:
                    p["exit_at"], p["reason"] = now + self.ow, "time"

    def _close(self, c, p, px, reason, taker, t):
        c.pos.remove(p)
        net = p["side"] * (px / p["entry"] - 1) * 1e4 - MAKER_BP - (TAKER_BP if taker else MAKER_BP)
        size = min(NOTIONAL, SHARE * p["through"])
        _append(self.dir / "fills.csv", FILL_COLS, {
            "trig_ms": p["trig_t"] if p["trig_t"] is not None else "", "fill_ms": p["fill_t"],
            "exit_ms": t, "coin": c.name, "vlm": vlm_bucket(c.vlm),
            "level_bp": p["lvl"], "side": p["side"], "ref": f"{p['ref']:.6g}", "entry": f"{p['entry']:.6g}",
            "exit": f"{px:.6g}", "reason": reason,
            "tf_ms": "" if p["trig_t"] is None else p["fill_t"] - p["trig_t"],
            "lag_ms": round(p["lag"]), "ow_ms": round(p["ow"]), "through_usd": round(p["through"]),
            "size_usd": round(size, 2), "net_bp": f"{net:.2f}", "pnl_usd": f"{size * net / 1e4:.4f}"})

    def save_state(self):
        st = {"since": self.since, **self.cnt, "updated": time.time() * 1000, "last_t": self.last_t,
              "lag_ms": round(self.lag), "ow_ms": round(self.ow), "coins": len(self.coins),
              "ladders_up": sum(c.up for c in self.coins.values()),
              "open": sum(len(c.pos) for c in self.coins.values())}
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, indent=1))
        tmp.replace(self.state_file)


# ---------------- report (daily Telegram + /wick) ----------------
def _rows(path):
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def report(datadir, now_ms=None, hours=None):
    d = Path(datadir)
    now_ms = now_ms or time.time() * 1000
    try:
        st = json.loads((d / "state.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    lo = now_ms - hours * 3_600_000 if hours else 0
    fills = [r for r in _rows(d / "fills.csv") if int(r["exit_ms"]) >= lo]
    lags = [r for r in _rows(d / "lag.csv") if int(r["minute_ms"]) >= lo]
    trigs = [r for r in _rows(d / "triggers.csv") if int(r["t_ms"]) >= lo]
    since = st.get("since") or now_ms
    days = max((now_ms - max(since, lo)) / 86_400_000, 1 / 24)
    span = f"last {hours}h" if hours else f"since {time.strftime('%d %b', time.gmtime(since / 1000))} ({days:.1f}d)"
    lines = [f"<b>Wick shadow</b> (paper, no orders) — {span}",
             f"ladders {LEVELS_BP[0]}/{LEVELS_BP[1]}bp, pulled on a {TRIG_BP}bp/5s trigger · ${NOTIONAL:.0f}/level, "
             f"{SHARE:.0%} of the volume through"]
    if not fills:
        lines.append("no fills yet")
    else:
        net = [float(r["net_bp"]) for r in fills]
        usd = sum(float(r["pnl_usd"]) for r in fills)
        lines.append(f"<b>{len(fills)} fills</b> ({len(fills) / days:.1f}/day) · mean {statistics.mean(net):+.1f}bp · "
                     f"win {sum(x > 0 for x in net) / len(net):.0%} · <b>${usd:+,.2f}</b> at test size")
        for lvl in LEVELS_BP:
            for b in ("$2-10M", "$10-50M", ">$50M"):
                sub = [float(r["net_bp"]) for r in fills if r["level_bp"] == str(lvl) and r["vlm"] == b]
                if sub:
                    lines.append(f"  +{lvl}bp {b}: {len(sub)} fills, {statistics.mean(sub):+.1f}bp, "
                                 f"win {sum(x > 0 for x in sub) / len(sub):.0%}")
        reasons = {k: sum(r["reason"] == k for r in fills) for k in ("tp", "time", "disconnect")}
        lines.append("exits: " + ", ".join(f"{k} {v}" for k, v in reasons.items() if v))
        best = sorted(fills, key=lambda r: float(r["pnl_usd"]))
        lines.append(f"best ${float(best[-1]['pnl_usd']):+.2f} ({best[-1]['coin']}), "
                     f"worst ${float(best[0]['pnl_usd']):+.2f} ({best[0]['coin']})")
        lines.append("<i>backtest at 0.6s: +200bp ≈ +41-45bp, +300bp ≈ +67-89bp per fill on $2M+ coins</i>")
    if trigs:
        lines.append(f"triggers {len(trigs)} ({sum(r['ladder_up'] == '1' for r in trigs)} with the ladder up)")
    if lags:
        p50 = statistics.median(float(r["p50"]) for r in lags)
        worst = max(lags, key=lambda r: float(r["p90"]))
        lines.append(f"feed lag: median {p50:.0f}ms · worst minute p90 {float(worst['p90']):.0f}ms / max "
                     f"{float(worst['max']):.0f}ms ({time.strftime('%d %b %H:%M', time.gmtime(int(worst['minute_ms']) / 1000))}) "
                     f"· order one-way {st.get('ow_ms', '?')}ms")
    if st:
        per = 1 / max((now_ms - since) / 86_400_000, 1 / 24)
        req = sum(st.get(k, 0) for k in ("orders", "modifies", "cancels")) * per
        lines.append(f"requests/day (address limit counts each order): {req:,.0f} = {st.get('orders', 0) * per:,.0f} places + "
                     f"{st.get('modifies', 0) * per:,.0f} modifies + {st.get('cancels', 0) * per:,.0f} cancels · "
                     f"lag pulls {st.get('lag_pulls', 0)}, disconnects {st.get('disconnects', 0)}")
        if st.get("updated"):
            lines.append(f"<i>{st.get('coins', 0)} coins, {st.get('ladders_up', 0)} ladders up, {st.get('open', 0)} open · "
                         f"updated {time.strftime('%H:%M:%S', time.gmtime(st['updated'] / 1000))} UTC</i>")
    return "\n".join(lines)


# ---------------- live loop ----------------
def universe():
    import tape_logger
    m = tape_logger.hl_post({"type": "metaAndAssetCtxs"})
    v = {u["name"]: float(c.get("dayNtlVlm") or 0) for u, c in zip(m[0]["universe"], m[1])
         if c.get("midPx") is not None and not u.get("isDelisted")}
    keep = sorted((k for k in v if v[k] >= MIN_VLM), key=v.get, reverse=True)[:MAX_COINS]
    return {k: v[k] for k in keep}


def rtt_probe(eng, every_s=30):
    """Keep eng.ow = half the median /info round trip over a kept-open connection."""
    conn, rt = None, deque(maxlen=10)
    body = json.dumps({"type": "l2Book", "coin": "BTC"})
    while True:
        try:
            if conn is None:
                conn = http.client.HTTPSConnection("api.hyperliquid.xyz", timeout=10)
            t = time.time()
            conn.request("POST", "/info", body=body, headers={"Content-Type": "application/json"})
            conn.getresponse().read()
            rt.append((time.time() - t) * 1000)
            eng.ow = statistics.median(rt) / 2
        except Exception:
            conn = None
        time.sleep(every_s)


def run_live(datadir):
    import tape_logger
    eng = Engine(datadir)
    threading.Thread(target=rtt_probe, args=(eng,), daemon=True).start()
    last_report = time.strftime("%Y%m%d", time.gmtime())
    tg.send("🪝 wick shadow online (paper, no orders) — /wick")
    backoff = 1
    while True:
        ws = tape_logger.WS()
        planned = False
        try:
            eng.vlm = universe()
            for k, v in eng.vlm.items():
                if k in eng.coins:
                    eng.coins[k].vlm = v
            ws.connect()
            ws.sock.settimeout(1.0)             # wake at least once a second for time exits
            for coin in eng.vlm:
                ws.send({"method": "subscribe", "subscription": {"type": "trades", "coin": coin}})
            subscribed, up_since = time.time(), time.time()
            print(f"subscribed to {len(eng.vlm)} coins", flush=True)
            backoff = 1
            while True:
                for msg in ws.messages() or []:
                    now = time.time() * 1000
                    try:
                        o = json.loads(msg)
                    except Exception:
                        continue
                    if o.get("channel") != "trades" or not o.get("data"):
                        continue
                    if time.time() - subscribed < 5:   # the snapshot of old trades sent on subscribe
                        continue
                    if not eng.connected:
                        eng.on_connect()
                    data = o["data"]
                    eng.on_lag(now, now - max(int(x["time"]) for x in data))
                    for x in data:
                        eng.on_trade(int(x["time"]), x["coin"], float(x["px"]), float(x["sz"]), now)
                now = time.time() * 1000
                eng.on_tick(now)
                day = time.strftime("%Y%m%d", time.gmtime())
                if day != last_report and time.gmtime().tm_min >= 5:
                    last_report = day
                    tg.send(report(datadir, hours=24))
                if time.time() - up_since > 86400:
                    planned = True
                    break                          # daily universe refresh
        except Exception as e:
            print(f"WS error: {e} -> reconnect in {backoff}s", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        finally:
            eng.on_disconnect(time.time() * 1000, planned)
            ws.close()


def run_replay(datadir, paths, lag_ms, rtt_ms, coins=None, repeg_bp=None):
    """Feed logged tape through the same engine with a fixed feed lag and order latency."""
    eng = Engine(datadir, vlm=coins, ow_ms=rtt_ms / 2, repeg_bp=repeg_bp)
    for path in paths:
        with (gzip.open if str(path).endswith(".gz") else open)(path, "rt", newline="") as f:
            r = csv.reader(f)
            next(r, None)
            for row in r:
                try:
                    t, coin, px, sz = int(row[0]), row[1], float(row[3]), float(row[4])
                except (ValueError, IndexError):
                    continue
                if coins is not None and coin not in coins:
                    continue
                now = t + lag_ms
                eng.on_lag(now, lag_ms)
                eng.on_trade(t, coin, px, sz, now)
                eng.on_tick(now)
    eng.flush_lag() if eng.lag_buf else eng.save_state()
    return eng


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--datadir", default="./wick")
    p.add_argument("--replay", nargs="*", help="tape files to replay instead of going live")
    p.add_argument("--lag-ms", type=float, default=400)
    p.add_argument("--rtt-ms", type=float, default=230)
    p.add_argument("--repeg-bp", type=float, help=f"move the ladder once the reference drifts this far (default {REPEG_MIN_BP})")
    p.add_argument("--all-coins", action="store_true", help="replay: every coin, not just today's $2M+ list")
    a = p.parse_args()
    if a.replay:
        eng = run_replay(a.datadir, a.replay, a.lag_ms, a.rtt_ms, None if a.all_coins else universe(), a.repeg_bp)
        print(report(a.datadir, now_ms=eng.last_t).replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    else:
        run_live(a.datadir)


if __name__ == "__main__":
    main()
