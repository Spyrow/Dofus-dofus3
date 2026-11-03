"""Local web UI: accounts list, browser login (PKCE), character / characteristics / equipment view.

  python webui/server.py [--port 8765]     then open http://127.0.0.1:8765

Bound to 127.0.0.1 only; Host/Origin are checked (DNS-rebinding / CSRF). Online: put an HTTPS proxy / tunnel in front, set the DOFUS_* variables
of auth.py (admin login, public host) and every page and API call then needs the admin session. Tokens never leave the server:
the API only returns labels and timestamps. Adding an account uses pkce_login (callback on port 9001, so
close the Ankama launcher first, and only one login can run at a time).
"""
import argparse, json, os, re, sys, threading, time
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import accounts, boost, gamesession, groq, questbook
sys.path.insert(0, HERE)
import auth
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

    def _hosts(self):
        port = self.server.server_port; pub = auth.public_host()
        return {f"127.0.0.1:{port}", f"localhost:{port}"} | ({pub} if pub else set())

    def _client(self):
        """Address of the visitor: the proxy / tunnel runs on this machine, so its forwarding header is trusted only from a loopback peer."""
        peer = self.client_address[0]
        if peer in ("127.0.0.1", "::1"):
            fwd = self.headers.get("CF-Connecting-IP") or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            if fwd: return fwd
        return peer

    def _redirect(self, where, cookie=None):
        self.send_response(303); self.send_header("Location", where); self.send_header("Content-Length", "0")
        if cookie: self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _cookie(self, value, max_age):
        secure = "; Secure" if auth.public_host() else ""
        return f"dofus_admin={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

    def _authed(self):
        if not auth.enabled(): return True
        jar = dict(kv.strip().split("=", 1) for kv in (self.headers.get("Cookie") or "").split(";") if "=" in kv)
        return auth.valid_cookie(jar.get("dofus_admin"))

    def _login_page(self, msg="", code=200):
        b = (auth.LOGIN_PAGE % msg).encode()
        self.send_response(code); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b)

    def _guard(self):
        host = self.headers.get("Host", "")
        if host not in self._hosts():
            self._json({"error": "bad host"}, 403); return False
        origins = {f"http://{host}", f"https://{host}"} if auth.public_host() and host == auth.public_host() else {f"http://{host}"}
        if self.command != "GET" and self.headers.get("Origin") not in origins:
            self._json({"error": "bad origin"}, 403); return False
        path = self.path.split("?")[0]
        if path in ("/login", "/logout"): return True
        if not self._authed():
            if self.command == "GET" and path in ("/", "/index.html"): self._redirect("/login")
            else: self._json({"error": "login required"}, 401)
            return False
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
        if p == "/login":
            if not auth.enabled() or self._authed(): return self._redirect("/")
            return self._login_page()
        if p == "/logout": return self._redirect("/login", self._cookie("", 0))
        if p in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f: b = f.read()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        elif p == "/api/accounts":
            self._json([DEMO] + [dict(a, session=gamesession.status(a["id"])) for a in accounts.list_accounts()])
        elif p == "/api/login":
            self._json(login_state)
        elif p == "/api/events":
            self._events()
        elif m := re.fullmatch(r"/api/accounts/(\w+)/character", p):
            aid = m[1]
            data = accounts.sample_character() if aid == "demo" else accounts.load_character(aid)
            if data is None: self._json({"error": "no_snapshot"}, 404)
            else: self._json(data)
        elif m := re.fullmatch(r"/api/accounts/(\w+)/boost", p):
            self._json(gamesession.session(m[1]).boost_view())
        elif m := re.fullmatch(r"/api/accounts/(\w+)/questreport", p):     # ?area=Incarnam : finished / active / still to do, with the scraped guide
            import questreport
            sess = gamesession.session(m[1]); area = (parse_qs(urlparse(self.path).query).get("area") or ["Incarnam"])[0]
            with sess._lock: fin = set(sess.finished_quests); act = set(sess.quests)
            self._json({"live": sess.status == "connected", "area": area, "finished_known": len(fin), **questreport.report(fin, act, area)})
        elif m := re.fullmatch(r"/api/accounts/(\w+)/quests", p):
            self._json(gamesession.session(m[1]).questview())
        elif p == "/api/groq":
            self._json({"configured": groq.configured(), "model": groq.model()})   # never the key
        elif p == "/api/questbook":
            self._json(questbook.summary())
        elif m := re.fullmatch(r"/api/accounts/(\w+)/recent", p):   # diagnostics: the last messages received from the game server
            self._json([{"t": ts, "type": t, "size": len(b), "hex": b.hex()[:80]} for ts, t, b in list(gamesession.session(m[1]).recent)[-80:]])
        elif m := re.fullmatch(r"/api/accounts/(\w+)/mapview", p):
            self._json(gamesession.session(m[1]).mapview())
        elif m := re.fullmatch(r"/api/accounts/(\w+)/session", p):
            q = dict(kv.split("=", 1) for kv in self.path.partition("?")[2].split("&") if "=" in kv)
            try: since = int(q.get("since", 0))
            except ValueError: since = 0
            self._json(gamesession.session(m[1]).view(since))
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
        p = self.path.split("?")[0]
        if p == "/login":
            if not auth.enabled(): return self._redirect("/")
            addr = self._client()
            if auth.throttled(addr): return self._login_page("Trop d'essais, réessayez dans quelques minutes.", 429)
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf8", "replace")).items()}
            if auth.check_login(addr, form.get("user"), form.get("password")): return self._redirect("/", self._cookie(auth.make_cookie(), auth.SESSION_SECONDS))
            return self._login_page("Identifiant ou mot de passe incorrect.", 401)
        if p == "/logout": return self._redirect("/login", self._cookie("", 0))
        body = self._body()
        if p == "/api/login":
            with login_lock:
                if login_state["status"] == "waiting": return self._json({"error": "login already in progress"}, 409)
                login_state.update(status="waiting", error=None)
            threading.Thread(target=_login_worker, args=(str(body.get("label") or "").strip()[:40],), daemon=True).start()
            self._json(login_state, 202)
        elif m := re.fullmatch(r"/api/accounts/(\w+)/(connect|disconnect)", p):
            if not accounts.get(m[1]): return self._json({"error": "not found"}, 404)
            sess = gamesession.session(m[1])
            if m[2] == "connect":
                if body.get("create"): sess.create_cfg = dict(gamesession.login.DEFAULT_CREATE, **{k: v for k, v in (body.get("create") if isinstance(body.get("create"), dict) else {}).items() if k in gamesession.login.DEFAULT_CREATE and k != "colors"})      # {"create": true} or {"create": {"breed": 3, "sex": 0}}: make a character if the account has none
                if not sess.connect(): return self._json({"error": "session déjà active"}, 409)
            else: sess.stop()
            self._json(sess.view(sess.seq), 202)
        elif p == "/api/groq":
            key = str(body.get("api_key") or "").strip(); mdl = str(body.get("model") or "").strip()
            if key and not re.fullmatch(r"[\w\-\.]{10,200}", key): return self._json({"error": "clé invalide"}, 400)
            if key or mdl: groq.save_config(key or None, mdl or None)
            self._json({"configured": groq.configured(), "model": groq.model()})
        elif m := re.fullmatch(r"/api/accounts/(\w+)/guide", p):
            text = str(body.get("text") or "").strip()[:300]
            if not text: return self._json({"error": "instruction vide"}, 400)
            err = gamesession.session(m[1]).auto.run_instruction(text)
            self._json({"error": err} if err else {"ok": True}, 409 if err else 202)
        elif m := re.fullmatch(r"/api/accounts/(\w+)/autopilot", p):
            sess = gamesession.session(m[1])
            if body.get("on"):
                quest_id = body.get("quest_id")
                if quest_id is not None and type(quest_id) is not int:
                    return self._json({"error": "identifiant de quête invalide"}, 400)
                if not sess.auto.start(body.get("kinds") if isinstance(body.get("kinds"), list) else None, bool(body.get("llm")), quest_id):
                    return self._json({"error": "impossible : personnage non connecté, autopilote déjà actif ou quête inactive"}, 409)
            else: sess.auto.stop()
            self._json({"ok": True}, 202)
        elif m := re.fullmatch(r"/api/accounts/(\w+)/attack", p):
            try: n = int(body.get("actor"))
            except (TypeError, ValueError): return self._json({"error": "bad request"}, 400)
            err = gamesession.session(m[1]).attack(n)
            self._json({"error": err} if err else {"ok": True}, 409 if err else 202)
        elif m := re.fullmatch(r"/api/accounts/(\w+)/useelement", p):
            try: n = int(body.get("element"))
            except (TypeError, ValueError): return self._json({"error": "bad request"}, 400)
            stand = body.get("stand"); stand = int(stand) if isinstance(stand, (int, float)) and 0 <= stand < 560 else None
            err = gamesession.session(m[1]).use_element(n, stand)
            self._json({"error": err} if err else {"ok": True}, 409 if err else 202)
        elif m := re.fullmatch(r"/api/accounts/(\w+)/achievements", p):
            sess = gamesession.session(m[1])
            if sess.status != "connected": return self._json({"error": "personnage non connecté"}, 409)
            sess.claim_achievements_later(); self._json({"ok": True}, 202)
        elif m := re.fullmatch(r"/api/accounts/(\w+)/equip", p):    # {"item": item id}
            try: n = int(body.get("item"))
            except (TypeError, ValueError): return self._json({"error": "bad request"}, 400)
            err = gamesession.session(m[1]).equip(n)
            self._json({"error": err} if err else {"ok": True}, 409 if err else 202)
        elif m := re.fullmatch(r"/api/accounts/(\w+)/(harvest|craft)", p):    # {"element": id} / {"item": result item id, "count": n}
            try:
                n = int(body.get("element" if m[2] == "harvest" else "item")); count = int(body.get("count") or 1)
            except (TypeError, ValueError): return self._json({"error": "bad request"}, 400)
            sess = gamesession.session(m[1])
            err = sess.harvest(n) if m[2] == "harvest" else sess.craft(n, max(1, min(count, 50)))
            self._json({"error": err} if err else {"ok": True}, 409 if err else 202)
        elif m := re.fullmatch(r"/api/accounts/(\w+)/boost", p):
            w = body.get("weights") if isinstance(body.get("weights"), dict) else None
            try: s = boost.save_settings(m[1], body.get("auto"), w) if ("auto" in body or w) else boost.load_settings(m[1])
            except (TypeError, ValueError): return self._json({"error": "paramètres invalides"}, 400)
            if body.get("run"):
                err = gamesession.session(m[1]).start_boost(w)
                if err: return self._json({"error": err}, 409)
            self._json(s, 202 if body.get("run") else 200)
        elif m := re.fullmatch(r"/api/accounts/(\w+)/syncquests", p):
            self._json({"finished": gamesession.session(m[1]).sync_quests()})
        elif m := re.fullmatch(r"/api/accounts/(\w+)/(talk|reply)", p):
            try: n = int(body.get("actor" if m[2] == "talk" else "id"))
            except (TypeError, ValueError): return self._json({"error": "bad request"}, 400)
            sess = gamesession.session(m[1])
            err = sess.talk(n) if m[2] == "talk" else sess.reply(n)
            self._json({"error": err} if err else {"ok": True}, 409 if err else 202)
        elif m := re.fullmatch(r"/api/accounts/(\w+)/(path|go|changemap)", p):
            sess = gamesession.session(m[1])
            if m[2] == "changemap":
                side = body.get("side")
                if side not in ("top", "bottom", "left", "right"): return self._json({"error": "bad side"}, 400)
                err = sess.change_map(side)
            else:
                try: cell = int(body.get("to"))
                except (TypeError, ValueError): return self._json({"error": "bad cell"}, 400)
                if not 0 <= cell < 560: return self._json({"error": "bad cell"}, 400)
                if m[2] == "path":
                    path, err = sess.plan(cell)
                    return self._json({"path": path} if path else {"error": err}, 200 if path else 422)
                err = sess.go(cell)
            self._json({"error": err} if err else {"ok": True}, 409 if err else 202)
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
    ap.add_argument("--host", default="127.0.0.1", help="bind address; keep 127.0.0.1 and let the HTTPS proxy / tunnel on this machine reach it")
    a = ap.parse_args()
    if a.host not in ("127.0.0.1", "localhost", "::1") and not auth.enabled(): sys.exit("refus : écouter hors de la machine exige DOFUS_ADMIN_PASSWORD_HASH (connexion admin)")
    if auth.public_host() and not auth.enabled(): sys.exit("refus : DOFUS_PUBLIC_HOST est défini sans DOFUS_ADMIN_PASSWORD_HASH")
    srv = ThreadingHTTPServer((a.host, a.port), H)
    print(f"http://{a.host}:{a.port}" + (f" (public : https://{auth.public_host()}, connexion admin activée)" if auth.enabled() else ""), file=sys.stderr)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass

if __name__ == "__main__":
    main()
