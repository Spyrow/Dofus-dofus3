"""Local web UI: accounts list, browser login (PKCE), character / characteristics / equipment view.

  python webui/server.py [--port 8765]     then open http://127.0.0.1:8765

Bound to 127.0.0.1 only; Host/Origin are checked (DNS-rebinding / CSRF). Tokens never leave the server:
the API only returns labels and timestamps. Adding an account uses pkce_login (callback on port 9001, so
close the Ankama launcher first, and only one login can run at a time).
"""
import argparse, json, os, re, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import accounts, gamesession
from pkce_login import login

DEMO = {"id": "demo", "label": "Démo (données fictives)", "added": None, "refreshed": None, "snapshot": None, "demo": True}
login_state = {"status": "idle", "error": None}   # idle | waiting | done | error
login_lock = threading.Lock()


def _login_worker(label):
    try:
        tok = login()
        accounts.add(label, tok["access_token"], tok.get("refresh_token"))
        login_state.update(status="done", error=None)
    except BaseException as e:                       # pkce_login uses sys.exit() on timeout
        login_state.update(status="error", error=str(e) or type(e).__name__)


connect_state = {}   # account id -> {status: idle | running | done | error, error}; one game session at a time
connect_lock = threading.Lock()


def _connect_worker(aid):
    try:
        gamesession.fetch_snapshot(aid)
        connect_state[aid] = {"status": "done", "error": None}
    except BaseException as e:                       # login helpers use SystemExit(msg) for expected failures
        connect_state[aid] = {"status": "error", "error": str(e) or type(e).__name__}
    finally:
        connect_lock.release()


SSE_HELLO, SSE_CHANGED, SSE_PING = (b"data: hello" + b"\n" * 2, b"data: changed" + b"\n" * 2, b": ping" + b"\n" * 2)


def _signature():
    """Changes whenever accounts.json or any character snapshot is written."""
    paths = [accounts.PATH] + ([os.path.join(accounts.CHAR_DIR, f) for f in os.listdir(accounts.CHAR_DIR)]
                               if os.path.isdir(accounts.CHAR_DIR) else [])
    sig = []
    for q in sorted(paths):
        try: sig.append((q, os.stat(q).st_mtime_ns))
        except OSError: pass
    return sig


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.send_header("Cache-Control", "no-store"); self.end_headers()
        self.wfile.write(b)

    def _guard(self):
        host = self.headers.get("Host", "")
        if host not in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"):
            self._json({"error": "bad host"}, 403); return False
        if self.command != "GET" and self.headers.get("Origin") not in (f"http://{host}",):
            self._json({"error": "bad origin"}, 403); return False
        return True

    def _events(self):
        """Server-sent events: one `data: changed` whenever the stored data changes (polled every 0.5 s)."""
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store"); self.end_headers()
        last, beat = _signature(), time.time()
        try:
            self.wfile.write(SSE_HELLO); self.wfile.flush()
            while True:
                time.sleep(0.5)
                cur = _signature()
                if cur != last: last, beat = cur, time.time(); self.wfile.write(SSE_CHANGED)
                elif time.time() - beat > 15: beat = time.time(); self.wfile.write(SSE_PING)
                else: continue
                self.wfile.flush()
        except OSError: pass                           # client went away

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try: return json.loads(self.rfile.read(n) or b"{}")
        except ValueError: return {}

    def do_GET(self):
        if not self._guard(): return
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f: b = f.read()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        elif p == "/api/accounts":
            self._json([DEMO] + accounts.list_accounts())
        elif p == "/api/login":
            self._json(login_state)
        elif p == "/api/events":
            self._events()
        elif m := re.fullmatch(r"/api/accounts/(\w+)/character", p):
            aid = m[1]
            data = accounts.sample_character() if aid == "demo" else accounts.load_character(aid)
            if data is None: self._json({"error": "no_snapshot"}, 404)
            else: self._json(data)
        elif m := re.fullmatch(r"/api/accounts/(\w+)/connect", p):
            self._json(connect_state.get(m[1], {"status": "idle", "error": None}))
        elif m := re.fullmatch(r"/api/accounts/(\w+)/ai", p):
            data = accounts.load_ai(m[1])
            self._json(data) if data is not None else self._json({"error": "no_ai"}, 404)
        else:
            self._json({"error": "not found"}, 404)

    def do_PUT(self):
        if not self._guard(): return
        m = re.fullmatch(r"/api/accounts/(\w+)/ai", self.path.split("?")[0])
        if not m: return self._json({"error": "not found"}, 404)
        if int(self.headers.get("Content-Length") or 0) > 65536: return self._json({"error": "too large"}, 413)
        r = accounts.save_ai(m[1], self._body())
        self._json(r) if r is not None else self._json({"error": "bad request"}, 400)

    def do_POST(self):
        if not self._guard(): return
        p, body = self.path.split("?")[0], self._body()
        if p == "/api/login":
            with login_lock:
                if login_state["status"] == "waiting": return self._json({"error": "login already in progress"}, 409)
                login_state.update(status="waiting", error=None)
            threading.Thread(target=_login_worker, args=(str(body.get("label") or "").strip()[:40],), daemon=True).start()
            self._json(login_state, 202)
        elif m := re.fullmatch(r"/api/accounts/(\w+)/connect", p):
            if not accounts.get(m[1]): return self._json({"error": "not found"}, 404)
            if not connect_lock.acquire(blocking=False): return self._json({"error": "une session de jeu est déjà en cours"}, 409)
            connect_state[m[1]] = {"status": "running", "error": None}
            threading.Thread(target=_connect_worker, args=(m[1],), daemon=True).start()
            self._json(connect_state[m[1]], 202)
        elif m := re.fullmatch(r"/api/accounts/(\w+)/refresh", p):
            try: r = accounts.refresh(m[1])
            except SystemExit as e: return self._json({"error": str(e)}, 502)
            except Exception as e: return self._json({"error": str(e)}, 502)
            self._json(r) if r else self._json({"error": "not found"}, 404)
        elif m := re.fullmatch(r"/api/accounts/(\w+)/rename", p):
            label = str(body.get("label") or "").strip()[:40]
            r = accounts.rename(m[1], label) if label else None
            self._json(r) if r else self._json({"error": "bad request"}, 400)
        else:
            self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        if not self._guard(): return
        m = re.fullmatch(r"/api/accounts/(\w+)", self.path.split("?")[0])
        ok = bool(m) and accounts.remove(m[1])
        self._json({"ok": ok}, 200 if ok else 404)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print(f"http://127.0.0.1:{a.port}", file=sys.stderr)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass

if __name__ == "__main__":
    main()
