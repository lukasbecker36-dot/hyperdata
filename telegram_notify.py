#!/usr/bin/env python3
"""
Tiny stdlib-only Telegram helper shared by the paper bot and the monitor.

Credentials come from the environment (never hard-code / commit them):
  TELEGRAM_BOT_TOKEN   token from @BotFather, e.g. 123456:ABC-DEF...
  TELEGRAM_CHAT_ID     your chat id (see get_chat_id() / DEPLOY.md)

send() NEVER raises: a Telegram outage or misconfig must never crash trading.
Returns True on success, False otherwise.
"""
import json
import os
import urllib.request
import urllib.parse

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

_API = "https://api.telegram.org/bot{token}/{method}"


def enabled():
    return bool(TOKEN and CHAT_ID)


def _call(method, params, tries=3, timeout=15):
    url = _API.format(token=TOKEN, method=method)
    data = urllib.parse.urlencode(params).encode()
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001  (best-effort; never propagate)
            last = e
    return {"ok": False, "error": str(last)}


def send(text, chat_id=None):
    """Send a message. Returns True on success, False otherwise. Never raises."""
    if not TOKEN:
        return False
    cid = chat_id or CHAT_ID
    if not cid:
        return False
    res = _call("sendMessage", {
        "chat_id": cid,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })
    return bool(res.get("ok"))


def set_commands(commands):
    """Register the command menu (autocomplete popup) with Telegram.

    `commands` is a list of (command, description) tuples; command has no
    leading slash. Returns True on success. Never raises.
    """
    if not TOKEN:
        return False
    payload = [{"command": c.lstrip("/"), "description": d} for c, d in commands]
    res = _call("setMyCommands", {"commands": json.dumps(payload)})
    return bool(res.get("ok"))


def get_updates(offset=None, timeout=25):
    """Long-poll for incoming messages (used by the monitor). Never raises."""
    if not TOKEN:
        return []
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    res = _call("getUpdates", params, tries=1, timeout=timeout + 10)
    return res.get("result", []) if res.get("ok") else []


if __name__ == "__main__":
    # Helper for setup: `python3 telegram_notify.py` prints the chat id(s) that
    # have messaged your bot. Message the bot first, then run this.
    import sys
    if not TOKEN:
        print("Set TELEGRAM_BOT_TOKEN first.", file=sys.stderr)
        sys.exit(1)
    ups = get_updates(timeout=2)
    if not ups:
        print("No updates. Send your bot a message (e.g. 'hi') first, then re-run.")
        sys.exit(0)
    seen = {}
    for u in ups:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat", {})
        if chat.get("id") is not None:
            seen[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name", "")
    for cid, name in seen.items():
        print(f"chat_id={cid}  ({name})")
