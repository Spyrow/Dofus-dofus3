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
import accounts, boost, challenges, gamesession, groq, questbook, knowledge
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
        elif p == "/api/trajet/catalog":      # jobs (gathering) with their resources, monsters with their zones
            import farmroute
            self._json(farmroute.catalog())
        elif p == "/api/farm/route":      # ?kind=monster|resource&id=..&acc=<account id>: the maps of the farm route, from the character's current map
            q = dict(kv.split("=", 1) for kv in self.path.partition("?")[2].split("&") if "=" in kv)
            try: ident = int(q.get("id", ""))
            except ValueError: return self._json({"error": "bad request"}, 400)
            import farmroute
            sess = gamesession._sessions.get(q.get("acc", ""))
            zone = int(q["zone"]) if q.get("zone", "").lstrip("-").isdigit() else None
            pods = None
            if sess and sess.status == "connected":
                with sess._lock: v = sess._vitals() or {}
                if v.get("pods_max") is not None and v.get("pods") is not None: pods = max(0, v["pods_max"] - v["pods"])
            self._json(farmroute.build(q.get("kind", "resource"), ident, sess.map_id if sess and sess.status == "connected" else None, zone, pods))
        elif m := re.fullmatch(r"/api/accounts/(\w+)/farm", p):      # progress of the running (or last) farm
            sess = gamesession._sessions.get(m[1]); f = sess.auto.farm if sess else None
            self._json({"farm": f, "on": bool(sess and sess.auto.on and f and f["state"] == "running"), "status": sess.status if sess else "disconnected", "busy": sess.busy if sess else None})
        elif p == "/api/market":      # priced items with the best average and where they come from (see client/market.py)
            import market
            self._json(market.table())
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
        elif m := re.fullmatch(r"/api/accounts/(\w+)/challenges", p):      # fight challenges: settings, state of the learned requests, the last fights
            self._json(gamesession.session(m[1]).challenge_view())
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
        elif m := re.fullmatch(r"/api/accounts/(\w+)/achievements", p):      # ?q=piou : unlocked achievements (the server only lists the unlocked ones, not the progress of the others)
            import gameachievements, knowledge
            sess = gamesession.session(m[1]); q = (parse_qs(urlparse(self.path).query).get("q") or [""])[0].casefold()
            rows = [{"id": i, "name": gameachievements.name(i), "reward_pending": bool(pend)} for i, pend in sorted(sess.achievements.items())]
            other = [{"id": i, "name": gameachievements.name(i), "by_characters": sorted(x for x in o if x)} for i, o in sorted(sess.achievements_account.items()) if i not in sess.achievements]
            prog = None
            try: ach = int((parse_qs(urlparse(self.path).query).get("id") or [0])[0])
            except ValueError: ach = 0
            if ach:
                cr = gameachievements.criteria(ach); kills = knowledge.load(f"kills_{sess.char_id}.json", {}) if sess.char_id else {}
                if cr:
                    import gamedata
                    mons = [{"monster": gamedata.quest_data("monsters", m) or m, "id": m, "killed_since_tracking": kills.get(str(m), 0)} for m in cr["monsters"]]
                    prog = {"achievement": gameachievements.name(ach), "validated": ach in sess.achievements, "minimum_level": cr["level"], "level_ok": (sess.level or 0) >= (cr["level"] or 0),
                            "monsters_to_kill_at_least_once": mons, "not_yet_seen_killed": [m["monster"] for m in mons if not m["killed_since_tracking"]], "unknown_criteria": cr["other"],
                            "note": "kills are counted from the moment this tracking was added; the server does not send the real progress"}
            self._json({"connected": sess.status == "connected", "character": sess.char_id, "progress": prog, "count": len(rows), "unlocked": [r for r in rows if q in (r["name"] or "").casefold()],
                        "other_characters_only": [r for r in other if q in (r["name"] or "").casefold()]})
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
        elif m := re.fullmatch(r"/api/accounts/(\w+)/guide_done", p):      # {"n": step number}: mark a manual step of the rush guide as done by hand
            try: n = int(body.get("n"))
            except (TypeError, ValueError): return self._json({"error": "bad request"}, 400)
            self._json({"done": sorted(gamesession.session(m[1]).auto._guided_progress(n))})
        elif m := re.fullmatch(r"/api/accounts/(\w+)/autopilot", p):
            sess = gamesession.session(m[1])
            if body.get("on"):
                quest_id = body.get("quest_id")
                if quest_id is not None and type(quest_id) is not int:
                    return self._json({"error": "identifiant de quête invalide"}, 400)
                sess.auto.guided = bool(body.get("guide"))      # strict order of the rush guide (guideorder.py)
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
            pos = body.get("position", 1)
            if type(pos) is not int or not 0 <= pos <= 20: return self._json({"error": "bad request"}, 400)
            err = gamesession.session(m[1]).equip(n, pos)
            self._json({"error": err} if err else {"ok": True}, 409 if err else 202)
        elif m := re.fullmatch(r"/api/accounts/(\w+)/farm", p):      # {"kind": "monster"|"resource", "id": n, "limit": n, "max_group": n} or {"stop": true}
            sess = gamesession.session(m[1])
            if body.get("stop"): sess.auto.stop(); return self._json({"ok": True}, 202)
            err = sess.auto.start_farm(body.get("kind"), body.get("id"), body.get("limit") or 0, body.get("max_group") or 4, body.get("zone"), bool(body.get("bank")))
            self._json({"error": err} if err else {"ok": True}, 409 if err else 202)
        elif m := re.fullmatch(r"/api/accounts/(\w+)/market", p):      # {"ids": [item ids] (default: every resource), "delay": seconds between requests}
            try: delay = max(0.3, float(body.get("delay") or 1.0)); ids = [int(i) for i in body["ids"]] if body.get("ids") else None
            except (TypeError, ValueError): return self._json({"error": "bad request"}, 400)
            err = gamesession.session(m[1]).market_scan_auto(ids, delay)
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
        elif m := re.fullmatch(r"/api/accounts/(\w+)/challenges", p):      # {"mode": auto|xp|drop|off, "choose": bool}
            mode, choose = body.get("mode"), body.get("choose")
            if mode is not None and mode not in challenges.MODES: return self._json({"error": "mode invalide"}, 400)
            self._json(challenges.save_settings(m[1], mode, choose if isinstance(choose, bool) else None))
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
    class Server(ThreadingHTTPServer):
        allow_reuse_address = os.name != "nt"      # on Windows SO_REUSEADDR lets a second server share the port and an old instance keeps answering
    try: srv = Server((a.host, a.port), H)
    except OSError as e: sys.exit(f"port {a.port} déjà utilisé ({e}) : fermez l'ancienne interface (python server.py) avant d'en lancer une autre")
    print(f"http://{a.host}:{a.port}" + (f" (public : https://{auth.public_host()}, connexion admin activée)" if auth.enabled() else ""), file=sys.stderr)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass

if __name__ == "__main__":
    main()
