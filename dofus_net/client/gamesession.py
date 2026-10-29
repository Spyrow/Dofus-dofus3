"""Persistent game session per UI account: refresh the HAAPI key, get a game token, log in, enter the character, then stay
connected (keep-alive like the real client) while decoding the pushed state (gamestate.py) into the account's snapshot.

Status: disconnected | connecting | connected | error. Each session keeps a journal (connection steps, actions, events)
and a chat buffer, both read by the web UI's Console tab. Needs the Shield certificate (shield.py) and the real game
closed (a second session on the same account kicks the first, in both directions).

Chat: the chat messages are NOT decoded yet (none in the captures so far): `Session.chat` stays empty until gamestate
learns the message; see add_chat()."""
import collections, threading, time
import accounts, shield, gamestate, gamedata, login
from haapi import create_game_token, GAME_IDS

KEEP = {gamestate.KSC, gamestate.KRG, gamestate.IRL, gamestate.IQX, gamestate.IPE, gamestate.ISM, gamestate.JOW}
HEARTBEAT = 5          # seconds, same cadence as the real client's kne
FLUSH = 2              # seconds between snapshot writes while connected


class Session:
    def __init__(self, aid):
        self.aid = aid
        self.status, self.error, self.since = "disconnected", None, None
        self.seq = 0
        self.journal = collections.deque(maxlen=800)
        self.chat = collections.deque(maxlen=400)
        self.character = self.map = None
        self._stop = threading.Event(); self._thread = None; self._lock = threading.RLock()

    # ---- journal / chat ----
    def log(self, level, text):
        with self._lock:
            self.seq += 1
            self.journal.append({"n": self.seq, "t": time.time(), "level": level, "msg": text})

    def add_chat(self, channel, author, text):
        with self._lock:
            self.seq += 1
            self.chat.append({"n": self.seq, "t": time.time(), "channel": channel, "author": author, "text": text})

    def set_status(self, status, error=None):
        with self._lock:
            if status != self.status: self.since = time.time()
            self.status, self.error = status, error
        self.log("error" if status == "error" else "info", {"connecting": "Connexion en cours…", "connected": "Personnage connecté",
                 "disconnected": "Déconnecté", "error": f"Erreur : {error}"}[status])

    def view(self, since=0):
        with self._lock:
            return {"status": self.status, "error": self.error, "since": self.since, "character": self.character, "map": self.map,
                    "seq": self.seq, "log": [e for e in self.journal if e["n"] > since], "chat": [e for e in self.chat if e["n"] > since]}

    # ---- lifecycle ----
    def start(self):
        with self._lock:
            if self.status in ("connecting", "connected"): return False
            self._stop = threading.Event(); self.status, self.error, self.since = "connecting", None, time.time()
        self.log("info", "Connexion en cours…")
        self._thread = threading.Thread(target=self._run, daemon=True); self._thread.start()
        return True

    def stop(self):
        if self.status in ("connecting", "connected"): self.log("action", "Déconnexion demandée")
        self._stop.set()

    def _run(self):
        c = None
        try:
            if not accounts.get(self.aid): raise SystemExit("compte inconnu")
            cert_id, cert_hash = shield.certificate_args()
            if not cert_hash: raise SystemExit("pas de certificat Shield : lance shield.py request puis shield.py validate <code>")
            self.log("info", "Rafraîchissement de la clé HAAPI"); accounts.refresh(self.aid)
            token = create_game_token(accounts.get(self.aid)["key"], GAME_IDS["dofus"], cert_id, cert_hash)
            self.log("info", "Jeton de jeu obtenu"); self.log("info", "Serveur de connexion : authentification")
            ticket, host = login.connection_step(token, cert_hash)
            self.log("info", f"Ticket obtenu, serveur de jeu {host}")
            state = {"msgs": [], "last_flush": 0.0, "dirty": False}

            def flush(force=False):
                if state["dirty"] and (force or time.time() - state["last_flush"] >= FLUSH):
                    snap = gamestate.snapshot(state["msgs"], host)
                    if snap.get("characteristics"): accounts.save_character(self.aid, snap)
                    state["dirty"], state["last_flush"] = False, time.time()

            def on_message(t, body):
                if t not in KEEP: return
                if t == gamestate.KRG: state["msgs"] = [m for m in state["msgs"] if m[0] != gamestate.KRG]   # the latest wins
                state["msgs"].append((t, body)); state["dirty"] = True
                if t == gamestate.KSC:
                    _cid, name = gamestate.selection(body)
                    with self._lock: self.character = name
                    self.log("event", f"Personnage : {name}")
                elif t == gamestate.KRG and self.status != "connected":
                    self.set_status("connected")
                elif t == gamestate.JOW:
                    mid = next((v for f, v in gamestate.parse(body) if f == 1 and isinstance(v, int)), None)
                    info = gamedata.map_info(mid) if mid else None
                    label = f"{info['subarea']} [{info['x']}, {info['y']}]" if info else f"carte {mid}"
                    with self._lock: self.map = label
                    self.log("event", f"Carte : {label}")
                flush()

            c = login.game_step(ticket, host, enter=True, idle=None, quiet=True, enter_map=True, stop=self._stop,
                                heartbeat=HEARTBEAT, on_message=on_message, log=self.log)
            flush(force=True)
            if self.status == "connecting": raise SystemExit("le serveur n'a envoyé aucune donnée de personnage")
            self.set_status("disconnected")
        except BaseException as e:                       # login helpers use SystemExit(msg) for expected failures
            self.set_status("error", str(e) or type(e).__name__)
        finally:
            if c is not None:
                try: c.s.close()
                except OSError: pass


_sessions = {}
_reg_lock = threading.Lock()


def session(aid):
    with _reg_lock:
        return _sessions.setdefault(aid, Session(aid))


def status(aid):
    s = _sessions.get(aid)
    return s.status if s else "disconnected"
