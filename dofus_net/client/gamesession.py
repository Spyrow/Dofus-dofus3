"""Persistent game session per UI account: refresh the HAAPI key, get a game token, log in, enter the character, then stay
connected (keep-alive like the real client) while decoding the pushed state (gamestate.py) into the account's snapshot.

Status: disconnected | connecting | connected | error. Each session keeps a journal (connection steps, actions, events)
and a chat buffer, both read by the web UI's Console tab. Needs the Shield certificate (shield.py) and the real game
closed (a second session on the same account kicks the first, in both directions).

Chat: the chat messages are NOT decoded yet (none in the captures so far): `Session.chat` stays empty until gamestate
learns the message; see add_chat()."""
import collections, threading, time
import accounts, shield, gamestate, gamedata, gamemaps, login
from wire import parse, dec_varint, enc_varint, f_bytes, f_varint, request
from haapi import create_game_token, GAME_IDS

KEEP = {login.JNJ, login.JNZ, gamestate.KSC, gamestate.KRG, gamestate.IRL, gamestate.IQX, gamestate.IPE, gamestate.ISM, gamestate.JOW}
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
        # current map state (reset on every jow): map id, our cell, other actors {id: cell}, our actor id
        self.map_id = self.my_cell = self.char_id = None; self.actors = {}; self.conn = None
        self._act = threading.Lock(); self.busy = None; self._moved = threading.Event(); self._mapchanged = threading.Event()
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

    # ---- map state (fed by the message loop) ----
    def _on_map_message(self, t, body):
        if t == gamestate.KSC:
            self.char_id = gamestate.selection(body)[0]
        elif t == gamestate.JOW:
            mid = next((v for f, v in parse(body) if f == 1 and isinstance(v, int)), None)
            with self._lock: self.map_id, self.my_cell, self.actors = mid, None, {}
            self._mapchanged.set()
        elif t == login.JNJ:      # {3: [{1: actor id, 2: {3: cell, 4: direction}}], 10: map id}
            for f, v in parse(body):
                if f != 3 or not isinstance(v, bytes): continue
                e = dict(parse(v)); aid = e.get(1)
                cell = next((c for f2, c in parse(e[2]) if f2 == 3 and isinstance(c, int)), None) if isinstance(e.get(2), bytes) else None
                if isinstance(aid, int) and cell is not None:
                    aid = aid - (1 << 64) if aid >= 1 << 63 else aid
                    with self._lock:
                        self.actors[aid] = cell
                        if aid == self.char_id: self.my_cell = cell
        elif t == login.JNZ:      # {1: packed path (cells), 4: actor id}
            e = dict(parse(body)); path = []; raw = e.get(1)
            if isinstance(raw, bytes):
                i = 0
                while i < len(raw):
                    v, i = dec_varint(raw, i); path.append(v & 0xFFF)
            aid = e.get(4)
            if path and isinstance(aid, int):
                aid = aid - (1 << 64) if aid >= 1 << 63 else aid
                with self._lock:
                    self.actors[aid] = path[-1]
                    if aid == self.char_id: self.my_cell = path[-1]; self._moved.set()

    def mapdata(self):
        return gamemaps.load(self.map_id) if self.map_id else None

    def mapview(self):
        """Everything the UI needs to draw the current map and its controls."""
        with self._lock: mid, me, actors, busy = self.map_id, self.my_cell, dict(self.actors), self.busy
        live = self.status == "connected" and mid is not None
        if mid is None:   # not connected: show the last known map from the snapshot
            snap = accounts.load_character(self.aid) or {}
            mid = (snap.get("position") or {}).get("map_id")
        m = gamemaps.load(mid) if mid else None
        info = gamedata.map_info(mid) if mid else None
        out = {"live": live, "map_id": mid, "info": info, "me": me, "busy": busy,
               "actors": {str(k): v for k, v in actors.items() if k != self.char_id}}
        if m:
            out["mov"] = "".join(str(c[0]) for c in m["cells"])
            near = gamedata.neighbours(mid)
            out["sides"] = {side: {"available": bool(m["neighbours"].get(side)) and bool(gamemaps.exit_cells(m, side)),
                                   "cells": gamemaps.exit_cells(m, side), "to": gamedata.map_info(near.get(side)) if near.get(side) else None}
                            for side in gamemaps.EXIT_BIT}
        return out

    def plan(self, to):
        """-> (cell path, error). The path avoids the other actors."""
        m = self.mapdata()
        if not m or self.my_cell is None: return None, "position inconnue (personnage non connecté ?)"
        blocked = {c for a, c in self.actors.items() if a != self.char_id}
        path = gamemaps.find_path(m, self.my_cell, int(to), blocked)
        return path, None if path else "aucun chemin"

    # ---- actions (one at a time, in a worker thread; progress goes to the journal) ----
    def _send(self, name, body=b""):
        if self.conn is None: raise RuntimeError("non connecté")
        self.conn.send(request(name, body))

    def _walk(self, path):
        """Send jok for `path`, wait for the server's jnz and the walking time, then confirm with jny."""
        keys = gamemaps.movement_keys(path)
        self._moved.clear()
        self._send(login.JOK, f_bytes(1, b"".join(enc_varint(k) for k in keys)) + f_varint(2, self.map_id))
        self.log("action", f"→ jok : {len(path) - 1} cases, de {path[0]} à {path[-1]}")
        if not self._moved.wait(3): raise RuntimeError("le serveur n'a pas confirmé le déplacement (jnz)")
        end = time.time() + gamemaps.duration(path)
        while time.time() < end:
            if self._stop.is_set(): raise RuntimeError("déconnecté")
            time.sleep(0.05)
        self._send(login.JNY); self.log("action", "→ jny (déplacement terminé)")

    def start_action(self, label, fn):
        """Run fn in a worker thread unless an action is already running. -> error string or None."""
        if self.status != "connected": return "personnage non connecté"
        if not self._act.acquire(blocking=False): return f"action en cours : {self.busy}"
        def run():
            try:
                with self._lock: self.busy = label
                self.log("action", label); fn(); self.log("info", f"Terminé : {label}")
            except BaseException as e:
                self.log("error", f"Échec : {label} — {e}")
            finally:
                with self._lock: self.busy = None
                self._act.release()
        threading.Thread(target=run, daemon=True).start()
        return None

    def go(self, to):
        def fn():
            path, err = self.plan(to)
            if err: raise RuntimeError(err)
            if len(path) > 1: self._walk(path)
        return self.start_action(f"Aller à la case {to}", fn)

    def change_map(self, side):
        def fn():
            m = self.mapdata(); target = (m or {}).get("neighbours", {}).get(side)
            if not m or not target: raise RuntimeError(f"pas de carte voisine ({side})")
            best = None
            for c in gamemaps.exit_cells(m, side):
                path, _ = self.plan(c)
                if path and (best is None or len(path) < len(best)): best = path
            if best is None: raise RuntimeError(f"aucune case de sortie atteignable ({side})")
            if len(best) > 1: self._walk(best)
            self._mapchanged.clear()
            self._send(login.JOC, f_varint(2, target)); self.log("action", f"→ joc : carte voisine {side} ({target})")
            if not self._mapchanged.wait(8): raise RuntimeError("le serveur n'a pas changé de carte (jow)")
        return self.start_action(f"Changer de carte ({side})", fn)

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
                self._on_map_message(t, body)
                if t in (login.JNJ, login.JNZ): return
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
                                heartbeat=HEARTBEAT, on_message=on_message, log=self.log,
                                on_ready=lambda cn: setattr(self, "conn", cn))
            flush(force=True)
            if self.status == "connecting": raise SystemExit("le serveur n'a envoyé aucune donnée de personnage")
            self.set_status("disconnected")
        except BaseException as e:                       # login helpers use SystemExit(msg) for expected failures
            self.set_status("error", str(e) or type(e).__name__)
        finally:
            self.conn = None
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
