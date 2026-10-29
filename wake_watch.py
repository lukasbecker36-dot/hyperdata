#!/usr/bin/env python3
"""
Wake-up watch -- alert (never trade) when an old, previously-spiked Aster coin starts
to look like AIN did hours before its 26 Oct 2026 run, then report what happened.

AIN, 26 Oct: price flat, but funding climbed from the +11%/yr default to +48-69%/yr
and MEXC spot offers within 5% thinned to ~2/3 of normal, hours before a 3.6x day.
Open interest and the scam score only moved after the price did. Requiring all of

    funding now > +30%/yr AND its last-4h average >= 25 points above its own
      baseline (median of the prints 12-36h earlier)       longs paying more
    MEXC ask depth within 5% <= 70% of its own 24h median   offers thinning
    |price change over 6h| < 10%                            not moved yet
    a +50% day within the last 30 days                      a spiker
    listed >= 60 days                                       old, "dead" coin

picked 4 coins in the first three days of logs: AIN (+262% within 24h), ARIA (+21%),
SIREN and BR (flat); $100 each made +$279. That filter was drawn while looking at AIN,
so it proves nothing yet. This collects the out-of-sample cases: each match is alerted
once (re-arm 72h), reported at 24h and 72h, and appended to wake_watch_results.csv;
the 72h report and /wake carry the running hit rate.

Two tighter variants are tagged on every alert and tallied separately (tags only, so they
are scored on the same cases): "flat" = price within +-5% of 6h ago (the first live alerts
often fired after a small run-up), "liquid" = Aster 24h volume >= $50k (on near-dead books
thin MEXC offers just mean nobody is quoting).

DATA: the basis-trade engine's hourly scam-screen logs (SCAM_LOG_DIR, default
/opt/basis-trade/output/scam_log_YYYYMMDD.csv[.gz]) -- funding, MEXC depth bands, marks,
30d best day and listing age for every Aster coin perp. Read-only; if that engine stops,
this goes quiet. Highs/lows are the hourly marks the sweeps saw.

  python3 wake_watch.py --datadir ./wakewatch            # run (Telegram via telegram_notify)
  python3 wake_watch.py --datadir ./wakewatch --once     # process the latest sweep and exit
"""
import argparse
import csv
import gzip
import json
import os
import statistics
import time
from pathlib import Path

import telegram_notify as tg

HOUR_MS = 3_600_000
LOG_DIR = Path(os.environ.get("SCAM_LOG_DIR", "/opt/basis-trade/output"))
FUND_MIN = 0.30          # funding now above +30%/yr (longs paying)
FUND_RISE = 0.25         # last-4h avg >= 25 points above its 12-36h-ago median
ASK_RATIO = 0.70         # MEXC ask depth within 5% <= 70% of its 24h median
MAX_MOVE = 0.10          # price within +-10% of 6h ago
PRIOR_SPIKE = 0.50       # a +50% day in the last 30 days
MIN_AGE_DAYS = 60.0
REARM_HOURS = 72.0
FLAT_MOVE = 0.05         # variant "flat": price within +-5% of 6h ago
LIQUID_VOL = 50_000.0    # variant "liquid": Aster 24h volume >= $50k
RESULT_COLS = ["signal_ms", "symbol", "entry", "max_24h", "min_24h", "ret_24h",
               "max_72h", "min_72h", "ret_72h", "flat", "liquid"]


def _num(v):
    try:
        return float(v) if v not in ("", None) else None
    except ValueError:
        return None


def load_history(now_ms, hours=40.0, log_dir=None):
    """symbol -> sweep rows from the last `hours`, oldest first."""
    log_dir = Path(log_dir or LOG_DIR)
    since = now_ms - hours * HOUR_MS
    days = {time.strftime("%Y%m%d", time.gmtime(t / 1000))
            for t in range(int(since), int(now_ms) + 86_400_000, 86_400_000) if t <= now_ms}
    days.add(time.strftime("%Y%m%d", time.gmtime(now_ms / 1000)))
    hist = {}
    for day in sorted(days):
        for path in (log_dir / f"scam_log_{day}.csv", log_dir / f"scam_log_{day}.csv.gz"):
            if not path.exists():
                continue
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt", newline="", encoding="utf-8", errors="replace") as f:   # CJK tickers
                for r in csv.DictReader(f):
                    ts = _num(r.get("ts_ms"))
                    if ts is None or ts < since or ts > now_ms:
                        continue
                    hist.setdefault(r["symbol"], []).append({
                        "ts": ts, "mark": _num(r.get("mark")), "fund": _num(r.get("funding_apr")),
                        "ask5": _num(r.get("mexc_ask_5pct")), "gain30": _num(r.get("max_day_gain_30d")),
                        "age": _num(r.get("age_days")), "score": _num(r.get("score")),
                        "vol": _num(r.get("perp_volume_usd"))})
    for rows in hist.values():
        rows.sort(key=lambda x: x["ts"])
    return hist


def latest_sweep_ms(log_dir=None):
    """Timestamp of the newest sweep in today's log (or 0)."""
    path = Path(log_dir or LOG_DIR) / f"scam_log_{time.strftime('%Y%m%d', time.gmtime())}.csv"
    if not path.exists():
        return 0
    last = 0
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            t = _num(r.get("ts_ms"))
            if t and t > last:
                last = t
    return int(last)


def features(rows, now_ms):
    """The filter's inputs at the latest sweep, or None without enough history."""
    if not rows:
        return None
    cur = rows[-1]
    if now_ms - cur["ts"] > 2 * HOUR_MS:
        return None

    def between(lo_h, hi_h, key):
        return [r[key] for r in rows
                if r[key] is not None and now_ms - hi_h * HOUR_MS <= r["ts"] <= now_ms - lo_h * HOUR_MS]

    recent = between(0, 3.5, "fund")
    base = between(12, 36, "fund")
    asks = between(4, 28, "ask5")
    then = [r for r in rows if r["mark"] and abs(r["ts"] - (now_ms - 6 * HOUR_MS)) <= 0.6 * HOUR_MS]
    if len(base) < 12 or len(asks) < 12 or not recent or not then or not cur["mark"]:
        return None
    med_ask = statistics.median(asks)
    return {
        "fund": cur["fund"], "d_fund": statistics.mean(recent) - statistics.median(base),
        "fund_base": statistics.median(base),
        "ask_ratio": (cur["ask5"] / med_ask) if cur["ask5"] is not None and med_ask > 0 else None,
        "px_chg6": cur["mark"] / then[0]["mark"] - 1, "gain30": cur["gain30"], "age": cur["age"],
        "mark": cur["mark"], "score": cur["score"], "vol": cur.get("vol")}


def matches(f):
    return bool(
        f and f["fund"] is not None and f["ask_ratio"] is not None
        and f["gain30"] is not None and f["age"] is not None
        and f["fund"] > FUND_MIN and f["d_fund"] >= FUND_RISE
        and f["ask_ratio"] <= ASK_RATIO and abs(f["px_chg6"]) < MAX_MOVE
        and f["gain30"] >= PRIOR_SPIKE and f["age"] >= MIN_AGE_DAYS)


def variants(f):
    """Which tighter variants this alert also passes."""
    return {"flat": abs(f["px_chg6"]) < FLAT_MOVE,
            "liquid": f.get("vol") is not None and f["vol"] >= LIQUID_VOL}


def tags(x):
    return " · ".join(f"{k} {'✓' if x.get(k) else '✗'}" for k in ("flat", "liquid"))


def _tally(rows):
    mx = [float(r["max_72h"]) for r in rows]
    r72 = [float(r["ret_72h"]) for r in rows]
    r24 = [float(r["ret_24h"]) for r in rows]
    return (f"{len(rows)} completed: {sum(m >= 0.3 for m in mx)} reached +30%, {sum(m >= 1.0 for m in mx)} "
            f"reached +100% within 72h; median 72h {statistics.median(r72):+.0%}. $100 each, "
            f"held 24h: ${100 * sum(r24):+,.0f}, held 72h: ${100 * sum(r72):+,.0f} (before costs)")


class Watch:
    def __init__(self, datadir):
        self.dir = Path(datadir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.dir / "wake_watch.json"
        self.results_file = self.dir / "wake_watch_results.csv"

    def load_state(self):
        try:
            st = json.loads(self.state_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            st = {}
        st.setdefault("active", {})
        st.setdefault("last_alert", {})
        st.setdefault("last_sweep", 0)
        return st

    def save_state(self, st):
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, indent=1))
        tmp.replace(self.state_file)

    def results(self):
        try:
            with open(self.results_file, newline="") as f:
                return list(csv.DictReader(f))
        except FileNotFoundError:
            return []

    def stats_line(self):
        rows = self.results()
        if not rows:
            return "no completed watches yet"
        out = [_tally(rows)]
        for lab, keep in (("flat (±5%/6h)", ("flat",)), ("liquid (≥$50k/day)", ("liquid",)),
                          ("flat+liquid", ("flat", "liquid"))):
            sub = [r for r in rows if all(r.get(k) == "1" for k in keep)]
            out.append(f"{lab}: " + (_tally(sub) if sub else "none completed"))
        return "\n".join(out)

    def step(self, now_ms, hist=None):
        """Process the sweep at now_ms: follow-ups for watched coins, then new matches.
        Returns the messages to send."""
        st = self.load_state()
        hist = load_history(now_ms) if hist is None else hist
        marks = {s: rows[-1]["mark"] for s, rows in hist.items()
                 if rows and rows[-1]["mark"] and now_ms - rows[-1]["ts"] <= 2 * HOUR_MS}
        msgs = []
        for sym, w in list(st["active"].items()):
            px = marks.get(sym)
            if px:
                w["hi"], w["lo"], w["last"] = max(w["hi"], px), min(w["lo"], px), px
            age_h = (now_ms - w["ts"]) / HOUR_MS
            if age_h >= 24 and "r24" not in w:
                w.update(r24=w["last"] / w["entry"] - 1, hi24=w["hi"] / w["entry"] - 1, lo24=w["lo"] / w["entry"] - 1)
                msgs.append(f"⏱ wake-watch {sym} after 24h: {w['r24']:+.0%} (max {w['hi24']:+.0%}, "
                            f"min {w['lo24']:+.0%}) from {w['entry']:.6g}")
            if age_h >= 72:
                row = {"signal_ms": int(w["ts"]), "symbol": sym, "entry": w["entry"],
                       "max_24h": w.get("hi24", w["hi"] / w["entry"] - 1),
                       "min_24h": w.get("lo24", w["lo"] / w["entry"] - 1),
                       "ret_24h": w.get("r24", w["last"] / w["entry"] - 1),
                       "max_72h": w["hi"] / w["entry"] - 1, "min_72h": w["lo"] / w["entry"] - 1,
                       "ret_72h": w["last"] / w["entry"] - 1,
                       "flat": int(bool(w.get("flat"))), "liquid": int(bool(w.get("liquid")))}
                new = not self.results_file.exists()
                with open(self.results_file, "a", newline="") as f:
                    wr = csv.DictWriter(f, fieldnames=RESULT_COLS)
                    if new:
                        wr.writeheader()
                    wr.writerow({k: (f"{v:.6g}" if isinstance(v, float) else v) for k, v in row.items()})
                del st["active"][sym]
                msgs.append(f"🏁 wake-watch {sym} after 72h: {row['ret_72h']:+.0%} (max {row['max_72h']:+.0%}, "
                            f"min {row['min_72h']:+.0%})\nrunning tally — {self.stats_line()}")
        for sym, rows in hist.items():
            if sym in st["active"] or now_ms - st["last_alert"].get(sym, 0) < REARM_HOURS * HOUR_MS:
                continue
            f = features(rows, now_ms)
            if not matches(f):
                continue
            st["last_alert"][sym] = now_ms
            st["active"][sym] = {"ts": now_ms, "entry": f["mark"], "hi": f["mark"], "lo": f["mark"], "last": f["mark"],
                                 **variants(f)}
            msgs.append(
                f"👀 <b>wake-up watch: {sym}</b> @ {f['mark']:.6g} — looks like AIN before its run\n"
                f"  funding {f['fund']:+.0%}/yr, up {f['d_fund']:+.0%} on its baseline {f['fund_base']:+.0%}\n"
                f"  MEXC offers within 5% at {f['ask_ratio']:.0%} of normal · price {f['px_chg6']:+.0%} over 6h\n"
                f"  +{f['gain30']:.0%} day in the last 30d · listed {f['age']:.0f}d · scam score {f['score']:.0f}\n"
                f"  Aster vol ${f.get('vol') or 0:,.0f}/24h · variants: {tags(st['active'][sym])}\n"
                f"  <i>WATCH ONLY: of the first 4 such cases 1 ran (AIN), 1 rose 21%, 2 went nowhere. "
                f"Reports at 24h and 72h.</i>\n{self.stats_line()}")
        st["last_sweep"] = now_ms
        self.save_state(st)
        return msgs


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--datadir", default="./wakewatch")
    p.add_argument("--once", action="store_true")
    a = p.parse_args()
    w = Watch(a.datadir)
    print(f"wake watch up | logs {LOG_DIR} | state {w.dir}", flush=True)
    while True:
        try:
            t = latest_sweep_ms()
            if t and t > w.load_state()["last_sweep"]:
                for m in w.step(t):
                    print(m.replace("\n", " | "), flush=True)
                    tg.send(m)
        except Exception as e:                       # never die on a bad log line
            print(f"step failed: {e}", flush=True)
        if a.once:
            break
        time.sleep(60)


if __name__ == "__main__":
    main()
