"""Persistent game session per UI account: refresh the HAAPI key, get a game token, log in, enter the character, then stay
connected (keep-alive like the real client) while decoding the pushed state (gamestate.py) into the account's snapshot.

Status: disconnected | connecting | connected | error. Each session keeps a journal (connection steps, actions, events)
and a chat buffer, both read by the web UI's Console tab. Needs the Shield certificate (shield.py) and the real game
closed (a second session on the same account kicks the first, in both directions).

Chat: the chat messages are NOT decoded yet (none in the captures so far): `Session.chat` stays empty until gamestate
learns the message; see add_chat()."""
import collections, threading, time
import accounts, shield, gamestate, gamedata, gamemaps, gamequests, gamedoors, autopilot, fightbot, login
from wire import parse, dec_varint, enc_varint, f_bytes, f_varint, request
from haapi import create_game_token, GAME_IDS

QUEST_MSGS = {login.ICA, login.ICJ, login.ICB, login.IBG, login.ILX, login.ILF, login.KIS}
FIGHT_MSGS = {login.JUO, login.JWJ, login.JVJ, login.JTE, login.JUG, login.JSY, login.JTK, login.JSQ, login.JXI, login.JWP}   # JWP was missing: the fight engine never saw the end of a fight
KEEP = {login.JNJ, login.JNS, login.JNZ, login.JXI, login.JWP} | QUEST_MSGS | FIGHT_MSGS | {gamestate.KSC, gamestate.KRG, gamestate.IRL, gamestate.IQX, gamestate.IPE, gamestate.ISM, gamestate.JOW}
HEARTBEAT = 5          # seconds, same cadence as the real client's kne
CONNECT_TIMEOUT = 45   # seconds without any character data before giving up
FLUSH = 2              # seconds between snapshot writes while connected


def _vi(body, num):
    return next((v for f, v in parse(body) if f == num and isinstance(v, int)), None)


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
        self.groups = {}; self.stats = {}; self.spell_ids = []; self.fight = fightbot.Fight(self)   # monster groups {actor: [monster ids]}
        self.elements = {}   # interactive elements of the current map (from jnj)
        self.in_fight = False; self.auto = autopilot.Autopilot(self)
        self.npcs = {}; self.quests = {}; self.dialog = None   # actor id -> template id; quest id -> (step, objectives); open dialog
        self._dialog_event = threading.Event(); self.recent = collections.deque(maxlen=200)   # (time, message type) of everything received
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
        if t in FIGHT_MSGS: self.fight.on_message(t, body)
        if t == gamestate.KRG:
            try: self.stats = gamestate.characteristics(body)
            except (ValueError, IndexError): pass
        elif t == gamestate.ISM:
            sl = gamestate.shortcut_spells(body)
            if sl: self.spell_ids = [sid for _slot, sid in sorted(sl)]
        if t == gamestate.KSC:
            self.char_id = gamestate.selection(body)[0]
        elif t == gamestate.JOW:
            mid = next((v for f, v in parse(body) if f == 1 and isinstance(v, int)), None)
            with self._lock: self.map_id, self.my_cell, self.actors, self.npcs, self.elements, self.groups = mid, None, {}, {}, {}, {}
            self._mapchanged.set()
        elif t in (login.JNJ, login.JNS):      # jnj {3: [entry]} / jns {1: entry}; entry = {1: actor id, 2: {3: cell, 4: direction}, 3: ...}
            for f, v in parse(body):
                if f != (3 if t == login.JNJ else 1) or not isinstance(v, bytes): continue
                self._register_actor(v)
            if t == login.JNJ:
                with self._lock: self.elements.update(gamedoors.elements(body))
        elif t == login.JXI:
            self.in_fight = True; self.log("event", "Combat engagé")
        elif t == login.JWP:
            self.in_fight = False; self.log("event", "Combat terminé")
        elif t == login.ICA:
            with self._lock: self.quests = {q: (st, objs) for q, st, objs in gamequests.active_quests(body)}
            self.log("event", f"Quêtes en cours : {len(self.quests)}")
        elif t == login.ICJ:
            q = gamequests.quest_state(body)
            if q:
                with self._lock: self.quests[q[0]] = (q[1], q[2])
                name = (gamedata.quest_data("quests", q[0]) or {}).get("name") or q[0]
                self.log("event", f"Quête « {name} » mise à jour : " + " ; ".join(f"{gamequests.objective_text(o)}{'' if a else ' ✓'}" for o, a in q[2].items()))
        elif t == login.ICB:
            qid = _vi(body, 1)
            name = (gamedata.quest_data("quests", qid) or {}).get("name") or qid
            self.log("event", f"Quête modifiée : « {name} »")
            try: self._send(login.IBV, f_varint(3, qid) + f_varint(4, -1))   # same as the real client: the server answers with icj
            except RuntimeError: pass
        elif t == login.IBG:
            oid, qid = _vi(body, 1), _vi(body, 2)
            with self._lock:
                if qid in self.quests and oid in self.quests[qid][1]: self.quests[qid][1][oid] = False   # done (the next icj confirms)
            self.log("event", f"Objectif accompli : {gamequests.objective_text(oid)}")
        elif t == login.ILX:
            with self._lock: self.dialog = {"actor": _vi(body, 5), "text": None, "replies": []}
        elif t == login.ILF:
            msg, replies = gamequests.dialog_step(body)
            text = gamedata.quest_data("messages", msg) or f"(message {msg})"
            reps = [{"id": r, "text": gamedata.quest_data("replies", r) or f"(réponse {r})"} for r in replies]
            with self._lock:
                d = self.dialog or {"actor": None}; d.update(text=text, replies=reps); self.dialog = d
            self.log("event", f"Dialogue : « {text[:120]} » — réponses : " + " | ".join(r["text"] for r in reps))
            self._dialog_event.set()
        elif t == login.KIS:
            with self._lock: self.dialog = None
            self.log("event", "Dialogue terminé"); self._dialog_event.set()
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

    def _register_actor(self, entry):
        e = dict(parse(entry)); aid = e.get(1)
        cell = next((c for f2, c in parse(e[2]) if f2 == 3 and isinstance(c, int)), None) if isinstance(e.get(2), bytes) else None
        if not isinstance(aid, int) or cell is None: return
        aid = aid - (1 << 64) if aid >= 1 << 63 else aid
        with self._lock:
            self.actors[aid] = cell
            if aid == self.char_id: self.my_cell = cell
            tpl = gamequests.npc_template(entry) if aid < 0 else None
            if tpl: self.npcs[aid] = tpl
            elif aid < 0 and not self.in_fight:
                mons = fightbot.monster_group(entry)
                if mons: self.groups[aid] = mons

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
        known = {e["element"]: e for e in gamedoors.from_map(mid)} if mid else {}
        out["elements"] = [{"element": k, "skill": v["skill"], "cell": v["cell"], "type": v["type"], "name": gamedata.get("interactives", v["interactive"]) if v["interactive"] is not None else None,
                            "to": gamedata.map_info(known[k]["to"]) if k in known else None} for k, v in list(self.elements.items())]
        out["groups"] = [{"actor": a, "cell": actors.get(a), "monsters": [{"id": m, "name": gamedata.quest_data("monsters", m) or f"monstre {m}"} for m in ms]}
                         for a, ms in list(self.groups.items())]
        out["in_fight"] = self.in_fight
        out["npcs"] = [{"actor": a, "cell": actors.get(a), "tpl": tpl, "name": gamedata.quest_data("npcs", tpl) or f"PNJ {tpl}"}
                       for a, tpl in list(self.npcs.items())]
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

    def _do_go(self, to):
        path, err = self.plan(to)
        if err: raise RuntimeError(err)
        if len(path) > 1: self._walk(path)

    def _do_change_map(self, side):
        m = self.mapdata(); target = (m or {}).get("neighbours", {}).get(side)
        if not m or not target: raise RuntimeError(f"pas de carte voisine ({side})")
        best = None
        for c in gamemaps.exit_cells(m, side):
            path, _ = self.plan(c)
            if path and (best is None or len(path) < len(best)): best = path
        if best is None: raise RuntimeError(f"aucune case de sortie atteignable ({side})")
        if len(best) > 1: self._walk(best)
        for attempt in (1, 2):   # the server sometimes ignores the first request right after a (re)connection: ask once more
            self._mapchanged.clear()
            self._send(login.JOC, f_varint(2, target)); self.log("action", f"→ joc : carte voisine {side} ({target})" + (" (2e essai)" if attempt == 2 else ""))
            if self._mapchanged.wait(8): break
            if attempt == 2: raise RuntimeError("le serveur n'a pas changé de carte (jow)")
            time.sleep(2.0)
        time.sleep(1.0)   # let the new map's actors arrive (jnj) before the next decision

    def go(self, to):
        return self.start_action(f"Aller à la case {to}", lambda: self._do_go(to))

    def change_map(self, side):
        return self.start_action(f"Changer de carte ({side})", lambda: self._do_change_map(side))

    def _do_use(self, element):
        """Walk next to an interactive element, use it (itz) and learn where it leads if the map changes."""
        e = self.elements.get(element)
        if not e: raise RuntimeError("élément introuvable sur la carte")
        frm = self.map_id
        known = next((d for d in gamedoors.from_map(frm) if d["element"] == element), None)
        if known and known.get("stand") is not None and self.my_cell != known["stand"]:   # walk where the real client stood to use it
            path, err = self.plan(known["stand"])
            if err: raise RuntimeError(f"case d'utilisation inaccessible : {err}")
            if len(path) > 1: self._walk(path)
        if e["cell"] is not None and self.my_cell != e["cell"]:   # the cell is unknown for now: the server is asked directly
            x, y = gamemaps.to_xy(e["cell"])
            adjacent = {gamemaps.from_xy(x + dx, y + dy) for dx, dy in gamemaps.DIRS} - {None}
            if self.my_cell not in adjacent:
                best = None
                for c in adjacent:
                    path, _ = self.plan(c)
                    if path and (best is None or len(path) < len(best)): best = path
                if best is None: raise RuntimeError("aucune case accessible à côté de l'élément")
                if len(best) > 1: self._walk(best)
        self._mapchanged.clear()
        self._send(login.ITZ, f_varint(1, e["skill"]) + f_varint(3, element))
        self.log("action", f"→ itz : élément {element} (compétence {e['skill']})")
        if self._mapchanged.wait(8):
            time.sleep(1.0)
            new = gamedoors.record(frm, e["skill"], element, e["cell"], self.map_id, known.get("stand") if known else None)
            info = gamedata.map_info(self.map_id)
            self.log("event", f"Passage {'appris' if new else 'connu'} : {frm} → {self.map_id}" + (f" ({info['subarea']} [{info['x']}, {info['y']}])" if info else ""))
        else:
            self.log("warn", "aucun changement de carte après l'utilisation de l'élément")

    def use_element(self, element):
        return self.start_action(f"Utiliser l'élément {element}", lambda: self._do_use(element))

    def _do_attack(self, actor):
        """Walk next to a monster group, attack it (hms) and play the whole fight with fightbot."""
        with self._lock: cell = self.actors.get(actor); mons = list(self.groups.get(actor, []))
        m = self.mapdata()
        if cell is None or not m: raise RuntimeError("groupe introuvable sur la carte")
        if len(mons) > fightbot.MAX_GROUP: raise RuntimeError(f"groupe trop gros ({len(mons)} monstres, max {fightbot.MAX_GROUP})")
        x, y = gamemaps.to_xy(cell)
        adjacent = {gamemaps.from_xy(x + dx, y + dy) for dx, dy in gamemaps.DIRS} - {None}
        if self.my_cell not in adjacent:
            best = None
            for c in adjacent:
                path, _ = self.plan(c)
                if path and (best is None or len(path) < len(best)): best = path
            if best is None: raise RuntimeError("aucune case accessible à côté du groupe")
            if len(best) > 1: self._walk(best)
        self._mapchanged.clear()
        self._send(login.HMS, f_varint(2, actor)); self.log("action", f"→ hms : attaquer le groupe {actor} ({', '.join(gamedata.quest_data('monsters', i) or str(i) for i in mons)})")
        if not self._mapchanged.wait(8): raise RuntimeError("le serveur n'a pas lancé le combat (pas de jow)")
        time.sleep(2.5)   # the real client waits for the fight map before placement
        self.fight.play()

    def attack(self, actor):
        return self.start_action(f"Attaquer le groupe {actor}", lambda: self._do_attack(actor))

    # ---- quests and dialogs ----
    def questview(self):
        with self._lock: quests, dialog, npcs, actors = dict(self.quests), dict(self.dialog) if self.dialog else None, dict(self.npcs), dict(self.actors)
        d = None
        if dialog:
            tpl = npcs.get(dialog.get("actor")); d = dict(dialog, npc=gamedata.quest_data("npcs", tpl) if tpl else None)
        return {"auto": {"on": self.auto.on, "goal": self.auto.goal}, "live": self.status == "connected", "quests": [gamequests.quest_view(q, st, objs) for q, (st, objs) in quests.items()], "dialog": d,
                "npcs": [{"actor": a, "cell": actors.get(a), "name": gamedata.quest_data("npcs", t) or f"PNJ {t}"} for a, t in npcs.items()]}

    def _do_talk(self, actor):
        """Walk next to the NPC, then send ilt (action 3 = Parler) and wait for the dialog to open."""
        with self._lock: cell = self.actors.get(actor); mid = self.map_id
        m = self.mapdata()
        if cell is None or not m: raise RuntimeError("PNJ introuvable sur la carte")
        x, y = gamemaps.to_xy(cell)
        adjacent = {gamemaps.from_xy(x + dx, y + dy) for dx, dy in gamemaps.DIRS} - {None}
        if self.my_cell not in adjacent:
            best = None
            for c in adjacent:
                path, _ = self.plan(c)
                if path and (best is None or len(path) < len(best)): best = path
            if best is None: raise RuntimeError("aucune case accessible à côté du PNJ")
            if len(best) > 1: self._walk(best)
        self._dialog_event.clear()
        self._send(login.ILT, f_varint(1, 3) + f_varint(2, mid) + f_varint(3, actor))
        self.log("action", f"→ ilt : parler au PNJ {actor}")
        t0 = time.time()
        if not self._dialog_event.wait(5):
            seen = {}
            for ts, t, b in self.recent:
                if ts >= t0 and str(t) not in seen and t not in ("knb", None): seen[str(t)] = b.hex()[:48]
            raise RuntimeError("le PNJ n'a pas répondu (reçu : " + ("; ".join(f"{t}={h or '-'}" for t, h in seen.items()) or "rien") + ")")

    def _do_reply(self, rid):
        if not self.dialog or rid not in [r["id"] for r in self.dialog.get("replies", [])]: raise RuntimeError("réponse indisponible")
        self._dialog_event.clear()
        self._send(login.IKY, f_varint(1, rid)); self.log("action", f"→ iky : réponse {rid}")
        if not self._dialog_event.wait(5): raise RuntimeError("pas de suite de dialogue")

    def talk(self, actor):
        return self.start_action(f"Parler au PNJ {actor}", lambda: self._do_talk(actor))

    def reply(self, rid):
        return self.start_action(f"Répondre ({rid})", lambda: self._do_reply(rid))

    # ---- lifecycle ----
    def start(self):
        with self._lock:
            if self.status in ("connecting", "connected"): return False
            self._stop = threading.Event(); self.status, self.error, self.since = "connecting", None, time.time()
        self.log("info", "Connexion en cours…")
        self._thread = threading.Thread(target=self._run, daemon=True); self._thread.start()
        def watchdog(stop=self._stop):   # the game server can stay silent (e.g. the character is still held by a previous session)
            time.sleep(CONNECT_TIMEOUT)
            if self.status == "connecting" and not stop.is_set():
                self.log("error", f"Aucune donnée de personnage après {CONNECT_TIMEOUT} s : le serveur ne répond pas"); stop.set()
        threading.Thread(target=watchdog, daemon=True).start()
        return True

    def stop(self):
        self.auto.stop()
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
                self.recent.append((time.time(), t, body))
                if t not in KEEP: return
                self._on_map_message(t, body)
                if t in (login.JNJ, login.JNS, login.JNZ, login.JXI, login.JWP) or t in QUEST_MSGS or t in FIGHT_MSGS: return
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
