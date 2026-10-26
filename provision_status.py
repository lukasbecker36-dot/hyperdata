#!/usr/bin/env python3
"""
Read-only status reporting for the LIVE provision executor.

Shared by two consumers so they can never disagree:
  * telegram_monitor.py  -- the /live, /open and /closed commands
  * provision_exec.py    -- its periodic summary

It only ever READS the executor's state.json / trades.csv and fetches public prices.
No credentials, no writes, no orders -- so a bug in here cannot affect trading.

Unrealised P&L is marked at the last traded price, which is what an exit would
approximately get. It is deliberately shown next to realised rather than folded into
it: winners exit on the +10% target within hours while losers sit to the 72h backstop,
so realised alone reads far too well early in a run.

    python3 provision_status.py            # all three reports
    python3 provision_status.py open       # one of: live | open | closed
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime, timezone

DIR = os.environ.get("PROVISION_DIR", "/opt/hyperdata/live_provision")
CLOSED_N = 20


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _state(d=None):
    p = os.path.join(d or DIR, "state.json")
    if not os.path.exists(p):
        return {}
    try:
        return json.load(open(p))
    except Exception:                        # noqa: BLE001
        return {}


def _trades(d=None):
    p = os.path.join(d or DIR, "trades.csv")
    if not os.path.exists(p):
        return []
    try:
        with open(p, newline="") as f:
            return list(csv.DictReader(f))
    except Exception:                        # noqa: BLE001
        return []


def _prices(symbols):
    """Public last prices. Returns {} on any failure -- never raises at a caller."""
    if not symbols:
        return {}
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import mexc_trade
        return {k: float(v) for k, v in mexc_trade.MexcSpot().prices().items()}
    except Exception:                        # noqa: BLE001
        return {}


def _age_h(ms):
    return (datetime.now(timezone.utc).timestamp() * 1000 - ms) / 3600000.0


def _mark(d=None):
    """(lots_with_marks, unrealised, exposure). lots carry px/unreal/pct/age."""
    s = _state(d)
    lots = s.get("lots", [])
    px = _prices([l["symbol"] for l in lots])
    out, unreal, expo = [], 0.0, 0.0
    for l in lots:
        cost = _f(l["cost_usd"])
        expo += cost
        p = px.get(l["symbol"])
        entry = _f(l["entry_px"])
        qty = _f(l["qty"])
        if p and entry > 0:
            val = p * qty
            u = val - cost
            pct = (p / entry - 1) * 100
        else:
            u, pct, val = 0.0, 0.0, cost
        unreal += u
        out.append({**l, "px": p, "value": val, "unreal": u, "pct": pct,
                    "age_h": _age_h(l["entry_ms"]),
                    "target": entry * 1.10})
    return out, unreal, expo


def live(d=None):
    s = _state(d)
    if not s:
        return "no live executor state found — is it running?"
    lots, unreal, expo = _mark(d)
    realised = _f(s.get("cum_pnl"))
    n_closed = int(s.get("n_closed", 0) or 0)
    n_win = int(s.get("n_win", 0) or 0)
    win = 100.0 * n_win / n_closed if n_closed else 0.0
    halted = os.path.exists(os.path.join(d or DIR, "HALT"))
    net = realised + unreal
    tr = _trades(d)
    since = tr[0]["entry_time"][:10] if tr else "—"
    lines = [
        "<b>LIVE provision</b>" + ("  ⛔ HALTED" if halted else ""),
        f"<b>net if closed now: ${net:+.2f}</b>",
        f"  realised   ${realised:+.2f}  ({n_closed} closed, {win:.0f}% win)",
        f"  unrealised ${unreal:+.2f}  ({len(lots)} open, ${expo:.2f} at risk)",
    ]
    if tr:
        wins = [t for t in tr if _f(t["pnl_usd"]) > 0]
        tgt = sum(1 for t in tr if t.get("reason") == "target")
        lines.append(f"  avg win ${sum(_f(t['pnl_usd']) for t in wins)/len(wins):+.2f}"
                     if wins else "  avg win —")
        lines.append(f"  target exits {100.0*tgt/len(tr):.0f}%   since {since}")
    cv = s.get("collective_vol")
    if cv:
        state = "NORMAL" if cv < 0.020 else "elevated"
        lines.append(f"collective vol {100*cv:.2f}%/h ({state}, normal &lt; 2.00%)")
    if lots:
        worst = min(lots, key=lambda x: x["unreal"])
        lines.append(f"  worst open: {worst['base']} {worst['pct']:+.1f}% "
                     f"(${worst['unreal']:+.2f})")
    return "\n".join(lines)


def open_positions(d=None):
    lots, unreal, expo = _mark(d)
    if not lots:
        return "no open positions"
    lots.sort(key=lambda x: x["unreal"])
    lines = [f"<b>open: {len(lots)}</b>  cost ${expo:.2f}  "
             f"unrealised <b>${unreal:+.2f}</b>", "<pre>"]
    lines.append(f"{'coin':<12}{'age':>5}{'move':>8}{'P&L':>8}")
    for l in lots:
        lines.append(f"{l['base'][:11]:<12}{l['age_h']:>4.0f}h"
                     f"{l['pct']:>7.1f}%{l['unreal']:>8.2f}")
    lines.append("</pre>")
    return "\n".join(lines)


def closed(d=None, n=CLOSED_N):
    tr = _trades(d)
    if not tr:
        return "no closed trades yet"
    last = tr[-n:]
    tot = sum(_f(t["pnl_usd"]) for t in last)
    wins = sum(1 for t in last if _f(t["pnl_usd"]) > 0)
    lines = [f"<b>last {len(last)} closed</b>  ${tot:+.2f}  "
             f"({wins}/{len(last)} winners)", "<pre>"]
    lines.append(f"{'coin':<11}{'ret':>7}{'P&L':>7}{'hrs':>5}  why")
    for t in last:
        why = {"target": "tgt", "time_stop": "72h", "stop": "stop"}.get(
            t.get("reason", ""), (t.get("reason") or "")[:4])
        lines.append(f"{t['symbol'].replace('USDT','')[:10]:<11}"
                     f"{_f(t['ret_pct']):>6.1f}%{_f(t['pnl_usd']):>7.2f}"
                     f"{_f(t['hold_h']):>5.0f}  {why}")
    lines.append("</pre>")
    cum = _f(tr[-1].get("cum_pnl"))
    lines.append(f"realised to date: ${cum:+.2f} over {len(tr)} trades")
    return "\n".join(lines)


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    import re
    strip = lambda s: re.sub(r"</?(b|pre)>", "", s)
    if which in ("all", "live"):
        print(strip(live()))
    if which in ("all", "open"):
        print("\n" + strip(open_positions()))
    if which in ("all", "closed"):
        print("\n" + strip(closed()))
