#!/usr/bin/env python3
"""
Interactive Telegram monitor for the Hyperliquid paper bot(s).

Runs as its own process (separate from trading, so it can never interfere with
the loop). Long-polls Telegram for commands and answers by READING the bot's
state/trades files. It never touches the exchange; the only thing it ever writes
is a small /adopt request flag in a live arm's datadir, which the live bot picks
up and acts on (the monitor itself never mutates positions).

Commands:
  /status      cum P&L, win rate, open count for every timeframe
  /pnl         same as /status (P&L focus)
  /positions   list of currently-open positions per timeframe
  /trades      last few closed trades per timeframe
  /adopt       tell the live arm to adopt any exchange position it is not managing
               (e.g. a maker entry that filled after the fill-detector timed out)
  /update      git pull + restart the bots (needs the sudoers rule, see DEPLOY §5)
  /help        this list

Config (env) additions:
  ADOPT_DATADIRS   optional "label:dir,..." override for which arms /adopt targets;
                   defaults to BOT_DATADIRS entries whose label starts with "LIVE".

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
import urllib.request
from datetime import datetime, timezone

import telegram_notify as tg

INFO_URL = "https://api.hyperliquid.xyz/info"
# Public account address, for reading accrued funding on open positions. Deliberately NOT
# sourced from /etc/hyperdata/live.env: that file holds the API wallet's PRIVATE KEY, and
# this is a read-only display bot that must never be able to sign. The address alone is
# public on-chain data and is passed as its own Environment= line in the unit.
HL_ADDR = os.environ.get("HL_ACCOUNT_ADDRESS", "").strip()
# An arm whose bot log has not been touched in this long is treated as stopped. Uses the
# LOG, not the state file: state is only written when a position opens or closes, so a
# quiet-but-running bot has a stale state file and would be wrongly reported dead.
STALE_MIN = 60

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
        bits = part.split(":")
        interval = bits[0].strip()
        d = bits[1].strip() if len(bits) > 1 else ""
        label = bits[2].strip() if len(bits) > 2 else interval
        out.append((interval, d, label))     # label = display name (defaults to interval)
    return out


def _mids():
    """coin -> mid price, in one public call. Returns {} on any failure so a rate limit
    or outage degrades the display instead of breaking the command."""
    try:
        req = urllib.request.Request(INFO_URL, data=json.dumps({"type": "allMids"}).encode(),
                                    headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return {k: float(v) for k, v in json.load(r).items()}
    except Exception:
        return {}


def _stale_min(datadir, interval):
    """Minutes since this arm's bot log was last written, or None if there is no log."""
    p = os.path.join(datadir, f"bot_{interval}.log")
    try:
        return (time.time() - os.path.getmtime(p)) / 60.0
    except Exception:
        return None


def _funding_open(addr, since_ms):
    """coin -> USD funding accrued since since_ms, in one call. {} on any failure.

    Realised P&L already carries funding: the bot books it per trade and cum_pnl includes
    the historical backfill. Open positions did not -- _unreal is mark-to-mid on price
    only -- so a position three hours into a hold was showing none of the carry it had
    already earned. On a 3.1h mean hold that is roughly three hourly settlements, and it
    is not always small: ACE and KAITO each accrued about $0.85 over the live book.
    """
    if not addr:
        return {}
    try:
        req = urllib.request.Request(
            INFO_URL,
            data=json.dumps({"type": "userFunding", "user": addr,
                             "startTime": int(since_ms)}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            ev = json.load(r)
        out = {}
        for x in ev or []:
            d = x.get("delta") or {}
            c = d.get("coin", "")
            if ":" in c or c.startswith("@"):        # xyz equity perps are not this bot's
                continue
            out[c] = out.get(c, 0.0) + float(d.get("usdc", 0) or 0)
        return out
    except Exception:
        return {}


def _unreal(p, mid):
    """Mark-to-mid P&L of one open position, in (usd, bps). Gross of the exit fee."""
    e = p.get("entry_px")
    if not e or not mid or mid <= 0:
        return (None, None)
    bps = p.get("dir", 0) * (mid - e) / e * 1e4
    return (p.get("notional", 0.0) * bps / 1e4, bps)


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


def _halt_paths():
    """(label, datadir) for every configured arm. HALT lives beside the state file."""
    return [(lab or iv, d) for iv, d, lab in _datadirs() if d]


def cmd_halt():
    """Manually block new entries on every arm. Exits are unaffected."""
    out = []
    for lab, d in _halt_paths():
        f = os.path.join(d, "HALT")
        if os.path.exists(f):
            out.append(f"<b>[{lab}]</b> already halted")
            continue
        try:
            with open(f, "w") as fh:
                json.dump({"tripped_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                                       time.gmtime()),
                           "reason": "manual /halt"}, fh, indent=1)
            out.append(f"<b>[{lab}]</b> HALTED - no new entries")
        except Exception as e:
            out.append(f"<b>[{lab}]</b> could not halt: {e}")
    out.append("<i>open positions still exit normally; use the KILL file to flatten</i>")
    return "\n".join(out)


def cmd_resume():
    """Clear the halt so entries resume. This is the only way back in after the
    drawdown brake trips -- deliberately manual, because an automatic reset would
    re-enter straight into whatever caused the loss."""
    out = []
    for lab, d in _halt_paths():
        f = os.path.join(d, "HALT")
        if not os.path.exists(f):
            out.append(f"<b>[{lab}]</b> not halted")
            continue
        try:
            info = json.load(open(f))
            why = f" (was: {info.get('reason','?')})"
        except Exception:
            why = ""
        try:
            os.remove(f)
            out.append(f"<b>[{lab}]</b> RESUMED - entries re-enabled{why}")
        except Exception as e:
            out.append(f"<b>[{lab}]</b> could not resume: {e}")
    return "\n".join(out)


def cmd_status():
    lines = []
    mids = _mids()
    for interval, d, label in _datadirs():
        s = _read_state(d, interval)
        if s is None:
            lines.append(f"<b>[{label}]</b> no state yet")
            continue
        closed = s.get("n_closed", 0)
        win = s.get("n_win", 0)
        wr = (win / closed * 100) if closed else 0.0
        cum = s.get("cum_pnl", 0.0)
        halted = os.path.exists(os.path.join(d, "HALT"))
        pos = s.get("positions", {})
        age = _stale_min(d, interval)
        stale = age is not None and age > STALE_MIN
        oldest = min((p.get("entry_ms", 0) for p in pos.values()), default=0)
        fnd = _funding_open(HL_ADDR, oldest - 1000) if (pos and oldest) else {}
        fu = sum(fnd.get(sym, 0.0) for sym in pos)
        u = sum(x for x in (_unreal(p, mids.get(sym))[0] for sym, p in pos.items())
                if x is not None) if (mids and not stale) else None
        tail = f" | {len(pos)} open" if not stale else f" | STOPPED {age/60:.1f}h"
        if u is not None and pos:
            tail += (f", unreal ${u:+.2f}" + (f" + fund ${fu:+.2f}" if fu else "")
                     + f" -> net ${cum+u+fu:+.2f}")
        lines.append(("⛔ " if halted else "") + f"<b>[{label}]</b> cum ${cum:+.2f} | "
                     f"{closed} closed, {wr:.0f}% win{tail}")
    return "\n".join(lines) or "no bots configured"


def cmd_positions():
    out = []
    mids = _mids()
    grand = 0.0
    for interval, d, label in _datadirs():
        s = _read_state(d, interval)
        pos = (s or {}).get("positions", {})
        age = _stale_min(d, interval)
        if age is not None and age > STALE_MIN:
            # bot not running: its state file is frozen, so any "open" positions here are
            # history, not exposure. Say so rather than showing phantom holds.
            out.append(f"<b>[{label}]</b> stopped {age/60:.1f}h ago"
                       + (f" — {len(pos)} position(s) frozen in state" if pos else ""))
            continue
        if not pos:
            out.append(f"<b>[{label}]</b> flat")
            continue
        oldest = min((p.get("entry_ms", 0) for p in pos.values()), default=0)
        fund = _funding_open(HL_ADDR, oldest - 1000) if oldest else {}
        tot, n_val, ftot, gross = 0.0, 0, 0.0, 0.0
        lines = []
        for sym, p in pos.items():
            side = "SHORT" if p.get("dir", 0) < 0 else "LONG"
            entry = p.get("entry_px")
            held_h = (int(time.time() * 1000) - p.get("entry_ms", 0)) / 3600000
            usd, bps = _unreal(p, mids.get(sym))
            fu = fund.get(sym)
            fs = f"  fund {fu:+.3f}" if fu else ""
            if fu:
                ftot += fu
            # notional is worth showing per line now that ats x pierce sizing spans
            # $12-$96: two positions in the same coin at the same price are no longer
            # the same bet, and gross exposure is the number margin is consumed against
            ntl = p.get("notional") or 0.0
            gross += ntl
            ns = f"  ${ntl:,.0f}" if ntl else ""
            if usd is None:
                lines.append(f"  {side} {sym} @ {entry:.6g}{ns}  ({held_h:.1f}h){fs}")
            else:
                tot += usd; n_val += 1
                lines.append(f"  {side} {sym} @ {entry:.6g}{ns}  ({held_h:.1f}h)  "
                             f"{usd:+.2f} ({bps:+.0f}b){fs}")
        hdr = f"<b>[{label}]</b> {len(pos)} open"
        if gross:
            hdr += f"  ${gross:,.0f} gross"
        if n_val:
            grand += tot + ftot
            hdr += f"  unreal {tot:+.2f}"
            if ftot:
                hdr += f" + fund {ftot:+.2f} = {tot+ftot:+.2f}"
        out.append(hdr + ":")
        out += lines
    if not mids:
        out.append("<i>(no prices — allMids call failed, so no unrealised shown)</i>")
    elif grand:
        out.append(f"<b>total open {grand:+.2f}</b>  "
                   f"(mark to mid + accrued funding, before exit fees)")
    return "\n".join(out) or "no bots configured"


def cmd_trades():
    out = []
    for interval, d, label in _datadirs():
        rows = _last_trades(d, interval, n=5)
        if not rows:
            out.append(f"<b>[{label}]</b> no trades yet")
            continue
        out.append(f"<b>[{label}]</b> last {len(rows)}:")
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


def _live_targets():
    """(label, datadir) for each LIVE arm to send an /adopt request to. From ADOPT_DATADIRS
    ('label:dir,label:dir') if set, else every BOT_DATADIRS entry whose label starts 'LIVE'.
    Paper arms are excluded: they have no exchange positions and never consume the flag."""
    spec = os.environ.get("ADOPT_DATADIRS", "").strip()
    out = []
    if spec:
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            bits = part.split(":")
            if len(bits) >= 2 and bits[1].strip():
                out.append((bits[0].strip(), bits[1].strip()))
            elif bits[0].strip():
                out.append((os.path.basename(bits[0].strip()), bits[0].strip()))
    else:
        for _interval, d, label in _datadirs():
            if d and label.upper().startswith("LIVE"):
                out.append((label, d))
    return out


def cmd_adopt():
    """Ask the live bot(s) to adopt any exchange position they are not managing.

    The monitor only READS state and never touches the exchange, so it cannot adopt
    directly (and injecting into the bot's state file would be overwritten on its next
    save). Instead it drops a request flag in each live arm's datadir; the bot picks it up
    within a few seconds, rebuilds the position from exchange truth, and reports here."""
    targets = _live_targets()
    if not targets:
        return ("no live arms found. Set ADOPT_DATADIRS=\"LIVE-ats:/opt/hyperdata/live_15m_ats\" "
                "on the telegram-monitor unit, or give the live arm a BOT_DATADIRS label "
                "starting with 'LIVE'.")
    done = []
    for label, d in targets:
        try:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "adopt_request.flag"), "w") as f:
                json.dump({"ts": int(time.time())}, f)
            done.append(label)
        except Exception as e:
            done.append(f"{label} (FAILED: {e})")
    return ("\U0001F527 adopt requested for: " + ", ".join(done) +
            "\nEach live bot scans the exchange for unmanaged positions within a few seconds "
            "and reports back here (or confirms it is flat).")


HELP = (
    "<b>Hyperliquid paper bot monitor</b>\n"
    "/status — P&amp;L + win rate + open count\n"
    "/pnl — same as /status\n"
    "/positions — currently open positions\n"
    "/trades — last few closed trades\n"
    "/adopt — adopt unmanaged exchange positions (live arm)\n"
    "/update — git pull + restart the bots\n"
    "/help — this message")

HANDLERS = {
    "/status": cmd_status,
    "/pnl": cmd_status,
    "/positions": cmd_positions,
    "/pos": cmd_positions,
    "/trades": cmd_trades,
    "/adopt": cmd_adopt,
    "/halt": cmd_halt,
    "/resume": cmd_resume,
    "/update": cmd_update,
    "/help": lambda: HELP,
    "/start": lambda: HELP,
}

# command menu (autocomplete popup); order shown in the client
MENU = [
    ("status", "P&L, win rate, open count"),
    ("positions", "currently open positions"),
    ("trades", "last few closed trades"),
    ("adopt", "adopt unmanaged exchange positions"),
    ("update", "git pull + restart the bots"),
    ("help", "list commands"),
]


def main():
    if not tg.enabled():
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")
    allowed = str(tg.CHAT_ID)
    print(f"monitor up | watching {[d for _, d, _ in _datadirs()]} | chat={allowed}", flush=True)
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
