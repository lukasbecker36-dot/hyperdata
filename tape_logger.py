#!/usr/bin/env python3
"""
Hyperliquid trade-tape logger — stdlib only (no pip), for forward VPIN / order-flow work.

The public REST API gives no historical tick tape (recentTrades is a tiny snapshot). The
live trades come only from the WebSocket `trades` channel, forward-only. This process
subscribes to `trades` for the whole active perp universe and appends each print to a daily
CSV, so that in a few weeks there is enough tape to backtest an order-flow-toxicity filter.

Each row: time_ms, coin, side (B=buy-aggressor / A=sell-aggressor), px, sz, tid
  -> side gives buy/sell classification directly (VPIN needs exactly this).

Implements a minimal RFC-6455 client over an ssl socket (server frames unmasked; our frames
masked). Auto-reconnects with backoff and resubscribes; refreshes the universe daily.

Run:  python3 tape_logger.py --datadir /opt/hyperdata/tape
"""
import argparse, base64, csv, gzip, json, os, shutil, socket, ssl, struct, sys, threading, time
import urllib.request
from datetime import datetime, timezone

INFO = "https://api.hyperliquid.xyz/info"
WS_HOST, WS_PORT, WS_PATH = "api.hyperliquid.xyz", 443, "/ws"
PING_EVERY_S = 30
FLUSH_EVERY_S = 5
UNIVERSE_REFRESH_S = 86400


def now_ms(): return int(time.time() * 1000)
def iso(ms=None): return datetime.fromtimestamp((ms or now_ms())/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def hl_post(body, tries=5):
    data = json.dumps(body).encode()
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(INFO, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            last = e; time.sleep(1.0 * (a + 1))
    raise last


def active_universe():
    """All active (non-delisted) perp names."""
    m = hl_post({"type": "metaAndAssetCtxs"})
    uni, ctxs = m[0]["universe"], m[1]
    return [u["name"] for u, c in zip(uni, ctxs) if c.get("midPx") is not None]


# ------------------------- minimal RFC-6455 client -------------------------
class WS:
    def __init__(self):
        self.sock = None
        self.buf = b""
        self.frag_op = None
        self.frag = b""

    def connect(self):
        raw = socket.create_connection((WS_HOST, WS_PORT), timeout=30)
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw, server_hostname=WS_HOST)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {WS_PATH} HTTP/1.1\r\nHost: {WS_HOST}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        # read handshake response headers
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk: raise ConnectionError("handshake: connection closed")
            resp += chunk
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"handshake failed: {resp.split(chr(13).encode(),1)[0]!r}")
        self.buf = resp.split(b"\r\n\r\n", 1)[1]     # any bytes after headers belong to frames
        self.sock.settimeout(PING_EVERY_S)
        self.frag_op = None; self.frag = b""

    def send(self, obj, opcode=0x1):
        payload = json.dumps(obj).encode()
        b0 = 0x80 | opcode
        n = len(payload)
        if n < 126:      header = struct.pack("!BB", b0, 0x80 | n)
        elif n < 65536:  header = struct.pack("!BBH", b0, 0x80 | 126, n)
        else:            header = struct.pack("!BBQ", b0, 0x80 | 127, n)
        mask = os.urandom(4)
        masked = bytes(payload[i] ^ mask[i % 4] for i in range(n))
        self.sock.sendall(header + mask + masked)

    def _parse_one(self):
        """Parse a single frame from self.buf; return (fin, opcode, payload) or None if incomplete."""
        b = self.buf
        if len(b) < 2: return None
        b0, b1 = b[0], b[1]
        fin = b0 & 0x80; opcode = b0 & 0x0F
        masked = b1 & 0x80; ln = b1 & 0x7F; idx = 2
        if ln == 126:
            if len(b) < 4: return None
            ln = struct.unpack("!H", b[2:4])[0]; idx = 4
        elif ln == 127:
            if len(b) < 10: return None
            ln = struct.unpack("!Q", b[2:10])[0]; idx = 10
        if masked:
            if len(b) < idx + 4: return None
            mask = b[idx:idx+4]; idx += 4
        if len(b) < idx + ln: return None
        payload = b[idx:idx+ln]
        if masked:
            payload = bytes(payload[i] ^ mask[i % 4] for i in range(ln))
        self.buf = b[idx+ln:]
        return fin, opcode, payload

    def messages(self):
        """Read available bytes, yield complete text messages; handle ping/pong/close/fragmentation."""
        try:
            data = self.sock.recv(65536)
            if not data: raise ConnectionError("closed by peer")
            self.buf += data
        except socket.timeout:
            self.send({"method": "ping"}); return    # app-level keepalive
        out = []
        while True:
            fr = self._parse_one()
            if fr is None: break
            fin, opcode, payload = fr
            if opcode == 0x8:                          # close
                raise ConnectionError("close frame")
            if opcode == 0x9:                          # ping -> pong
                self._pong(payload); continue
            if opcode == 0xA:                          # pong
                continue
            if opcode in (0x1, 0x2):                   # start of a data message
                self.frag_op, self.frag = opcode, payload
            elif opcode == 0x0:                        # continuation
                self.frag += payload
            if fin and self.frag_op is not None:
                try: out.append(self.frag.decode("utf-8", "replace"))
                finally: self.frag_op, self.frag = None, b""
        for m in out: yield m

    def _pong(self, payload):
        b0 = 0x80 | 0xA; n = len(payload); mask = os.urandom(4)
        header = struct.pack("!BB", b0, 0x80 | n) if n < 126 else struct.pack("!BBH", b0, 0x80 | 126, n)
        self.sock.sendall(header + mask + bytes(payload[i] ^ mask[i % 4] for i in range(n)))

    def close(self):
        try: self.sock.close()
        except Exception: pass


# ------------------------- logger -------------------------
class TapeLogger:
    def __init__(self, datadir):
        self.datadir = datadir
        os.makedirs(datadir, exist_ok=True)
        self.log_file = os.path.join(datadir, "tape.log")
        self.day = None; self.fh = None; self.writer = None; self.cur_path = None
        self.n_rows = 0; self.last_flush = 0.0; self.last_stat = 0.0

    def log(self, msg):
        line = f"{iso()}  {msg}"; print(line, flush=True)
        with open(self.log_file, "a") as f: f.write(line + "\n")

    def _compress(self, path):
        """gzip a finished daily file and drop the plain .csv (crash-safe: .csv removed last)."""
        try:
            gz = path + ".gz"
            with open(path, "rb") as fin, gzip.open(gz, "wb", compresslevel=6) as fout:
                shutil.copyfileobj(fin, fout, length=1 << 20)
            os.remove(path)
            self.log(f"compressed {os.path.basename(path)} -> {os.path.basename(gz)} "
                     f"({os.path.getsize(gz)/1e6:.1f} MB)")
        except Exception as e:
            self.log(f"WARN compress {os.path.basename(path)}: {e}")

    def _compress_stale(self):
        """On startup, gzip any leftover finished-day .csv files (e.g. from a crash mid-day)."""
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        for fn in sorted(os.listdir(self.datadir)):
            if fn.startswith("tape_") and fn.endswith(".csv") and fn != f"tape_{today}.csv":
                self._compress(os.path.join(self.datadir, fn))

    def _roll(self):
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        if day != self.day:
            if self.fh:
                self.fh.flush(); self.fh.close()
                # gzip the just-finished day in the background so capture never blocks
                threading.Thread(target=self._compress, args=(self.cur_path,), daemon=True).start()
            path = os.path.join(self.datadir, f"tape_{day}.csv")
            new = not os.path.exists(path)
            self.fh = open(path, "a", newline="")
            self.writer = csv.writer(self.fh)
            if new: self.writer.writerow(["time_ms", "coin", "side", "px", "sz", "tid"])
            self.day = day; self.cur_path = path
            self.log(f"writing {path}")

    def write_trades(self, trades):
        self._roll()
        for t in trades:
            self.writer.writerow([t.get("time"), t.get("coin"), t.get("side"),
                                  t.get("px"), t.get("sz"), t.get("tid")])
            self.n_rows += 1
        now = time.time()
        if now - self.last_flush > FLUSH_EVERY_S:
            self.fh.flush(); self.last_flush = now
        if now - self.last_stat > 60:
            self.log(f"heartbeat: {self.n_rows} trades logged so far"); self.last_stat = now

    def run(self):
        self.log("=== tape logger starting (stdlib WS, trades channel) ===")
        self._compress_stale()          # tidy up any uncompressed finished days from a prior run
        backoff = 1
        universe = []; uni_ts = 0
        while True:
            ws = WS()
            try:
                if not universe or time.time() - uni_ts > UNIVERSE_REFRESH_S:
                    universe = active_universe(); uni_ts = time.time()
                    self.log(f"universe: {len(universe)} active perps")
                ws.connect()
                for coin in universe:
                    ws.send({"method": "subscribe", "subscription": {"type": "trades", "coin": coin}})
                self.log(f"subscribed to trades for {len(universe)} coins")
                backoff = 1
                while True:
                    for msg in ws.messages() or []:
                        try: obj = json.loads(msg)
                        except Exception: continue
                        if obj.get("channel") == "trades":
                            data = obj.get("data") or []
                            if data: self.write_trades(data)
                    # daily universe refresh triggers a reconnect+resubscribe
                    if time.time() - uni_ts > UNIVERSE_REFRESH_S:
                        self.log("daily universe refresh -> reconnecting"); break
            except Exception as e:
                self.log(f"WS error: {e} -> reconnect in {backoff}s")
                time.sleep(backoff); backoff = min(backoff * 2, 60)
            finally:
                ws.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--datadir", default="./tape")
    a = ap.parse_args()
    TapeLogger(a.datadir).run()
