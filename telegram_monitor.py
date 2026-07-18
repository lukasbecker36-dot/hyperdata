#!/usr/bin/env python3
"""
Interactive Telegram monitor for the Hyperliquid paper bot(s).

Runs as its own process (separate from trading, so it can never interfere with
the loop). Long-polls Telegram for commands and answers by READING the bot's
state/trades files — it never writes anything and never touches the exchange.

Commands:
  /status      cum P&L, win rate, open count for every timeframe
  /pnl         same as /status (P&L focus)
  /positions   list of currently-open positions per timeframe
  /trades      last few closed trades per timeframe
  /update      git pull + restart the bots (needs the sudoers rule, see DEPLOY §5)
  /help        this list

Config (env):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (shared with the bot)
  BOT_DATADIRS   comma-separated "interval:dir" pairs
                 default: "5m:./paper_5m,15m:./paper_15m"
  REPO_DIR       git repo to pull for /update      (default: /opt/hyperdata)
  RESTART_UNITS  space-separated systemd bot units (default: "paper-bot-5m paper-bot-15m")
  SYSTEMCTL      path to systemctl                 (default: /usr/bin/systemctl)

Only messages from TELEGRAM_CHAT_ID are answered.
"""
import csv
import json
import os
import subprocess
import time
from datetime import datetime, timezone

import telegram_notify as tg

REPO_DIR = os.environ.get("REPO_DIR", "/opt/hyperdata")
RESTART_UNITS = os.environ.get("RESTART_UNITS", "paper-bot-5m paper-bot-15m").split()
SYSTEMCTL = os.environ.get("SYSTEMCTL", "/usr/bin/systemctl")
SELF_UNIT = os.environ.get("SELF_UNIT", "telegram-monitor")


def _datadirs():
    spec = os.environ.get("BOT_DATADIRS", "5m:./paper_5m,15m:./paper_15m")
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        interval, _, d = part.partition(":")
        out.append((interval.strip(), d.strip()))
    return out


def _read_state(datadir, interval):
    path = os.path.join(datadir, f"state_{interval}.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _last_trades(datadir, interval, n=5):
    path = os.path.join(datadir, f"trades_{interval}.csv")
    try:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        return rows[-n:]
    except Exception:
        return []


def cmd_status():
    lines = []
    for interval, d in _datadirs():
        s = _read_state(d, interval)
        if s is None:
            lines.append(f"<b>[{interval}]</b> no state yet")
            continue
        closed = s.get("n_closed", 0)
        win = s.get("n_win", 0)
        wr = (win / closed * 100) if closed else 0.0
        cum = s.get("cum_pnl", 0.0)
        nopen = len(s.get("positions", {}))
        lines.append(
            f"<b>[{interval}]</b> cum ${cum:+.2f} | "
            f"{closed} closed, {wr:.0f}% win | {nopen} open")
    return "\n".join(lines) or "no bots configured"


def cmd_positions():
    out = []
    for interval, d in _datadirs():
        s = _read_state(d, interval)
        pos = (s or {}).get("positions", {})
        if not pos:
            out.append(f"<b>[{interval}]</b> flat")
            continue
        out.append(f"<b>[{interval}]</b> {len(pos)} open:")
        for sym, p in pos.items():
            side = "SHORT" if p.get("dir", 0) < 0 else "LONG"
            entry = p.get("entry_px")
            held_h = (int(time.time() * 1000) - p.get("entry_ms", 0)) / 3600000
            out.append(f"  {side} {sym} @ {entry:.6g}  ({held_h:.1f}h)")
    return "\n".join(out) or "no bots configured"


def cmd_trades():
    out = []
    for interval, d in _datadirs():
        rows = _last_trades(d, interval, n=5)
        if not rows:
            out.append(f"<b>[{interval}]</b> no trades yet")
            continue
        out.append(f"<b>[{interval}]</b> last {len(rows)}:")
        for r in rows:
            out.append(
                f"  {r.get('symbol','?')} {r.get('side','?')} "
                f"{r.get('net_bps','?')}bps ${r.get('pnl_usd','?')} ({r.get('reason','?')})")
    return "\n".join(out) or "no bots configured"


def _run(cmd, cwd=None, timeout=120):
    """Run a command, return (ok, combined_output). Never raises."""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout + p.stderr).strip()
        return p.returncode == 0, out
    except Exception as e:
        return False, str(e)


def cmd_update():
    """git pull + restart the bot units. Restarts the monitor last (detached)
    only if the pull actually changed anything."""
    tg.send("\U0001F504 updating: pulling latest ...")
    ok, out = _run(["git", "pull", "--ff-only"], cwd=REPO_DIR)
    tail = "\n".join(out.splitlines()[-6:]) or "(no output)"
    if not ok:
        return f"❌ git pull failed:\n<pre>{tail}</pre>"
    changed = "Already up to date" not in out and "Already up-to-date" not in out

    results = []
    for unit in RESTART_UNITS:
        rok, rout = _run(["sudo", "-n", SYSTEMCTL, "restart", unit])
        results.append(f"  {'✅' if rok else '❌'} {unit}"
                       + ("" if rok else f": {rout.splitlines()[-1] if rout else '?'}"))
    body = (f"\U0001F4E5 <b>update</b> — {'changes pulled' if changed else 'already current'}\n"
            f"<pre>{tail}</pre>\n"
            "restarted:\n" + "\n".join(results))

    # If code changed, refresh the monitor too — detached, so this reply still sends.
    if changed:
        body += f"\n♻ restarting {SELF_UNIT} (you'll get a fresh 'monitor online')"
        tg.send(body)
        try:
            subprocess.Popen(["sudo", "-n", SYSTEMCTL, "restart", SELF_UNIT],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        return None      # already sent
    return body


HELP = (
    "<b>Hyperliquid paper bot monitor</b>\n"
    "/status — P&amp;L + win rate + open count\n"
    "/pnl — same as /status\n"
    "/positions — currently open positions\n"
    "/trades — last few closed trades\n"
    "/update — git pull + restart the bots\n"
    "/help — this message")

HANDLERS = {
    "/status": cmd_status,
    "/pnl": cmd_status,
    "/positions": cmd_positions,
    "/pos": cmd_positions,
    "/trades": cmd_trades,
    "/update": cmd_update,
    "/help": lambda: HELP,
    "/start": lambda: HELP,
}

# command menu (autocomplete popup); order shown in the client
MENU = [
    ("status", "P&L, win rate, open count"),
    ("positions", "currently open positions"),
    ("trades", "last few closed trades"),
    ("update", "git pull + restart the bots"),
    ("help", "list commands"),
]


def main():
    if not tg.enabled():
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")
    allowed = str(tg.CHAT_ID)
    print(f"monitor up | watching {[d for _, d in _datadirs()]} | chat={allowed}", flush=True)
    tg.set_commands(MENU)      # register the autocomplete menu
    tg.send("\U0001F4F1 monitor online — send /help")
    offset = None
    while True:
        try:
            updates = tg.get_updates(offset=offset, timeout=25)
        except Exception:
            time.sleep(3)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or u.get("edited_message") or {}
            chat = msg.get("chat", {})
            if str(chat.get("id")) != allowed:      # ignore everyone else
                continue
            text = (msg.get("text") or "").strip().lower()
            cmd = text.split()[0] if text else ""
            cmd = cmd.split("@")[0]                  # strip @botname in groups
            handler = HANDLERS.get(cmd)
            if handler:
                try:
                    reply = handler()
                    if reply is not None:      # None = handler already sent its own message(s)
                        tg.send(reply)
                except Exception as e:
                    tg.send(f"error: {e}")
            elif text.startswith("/"):
                tg.send("unknown command — /help")


if __name__ == "__main__":
    main()
