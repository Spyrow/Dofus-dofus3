"""Persistent game session per UI account: refresh the HAAPI key, get a game token, log in, enter the character, then stay
connected (keep-alive like the real client) while decoding the pushed state (gamestate.py) into the account's snapshot.

Status: disconnected | connecting | connected | error. Each session keeps a journal (connection steps, actions, events)
and a chat buffer, both read by the web UI's Console tab. Needs the Shield certificate (shield.py) and the real game
closed (a second session on the same account kicks the first, in both directions).

Chat: the chat messages are NOT decoded yet (none in the captures so far): `Session.chat` stays empty until gamestate
learns the message; see add_chat()."""
import collections, json, os, threading, time
import boost, knowledge, accounts, shield, gamestate, gamedata, gamemaps, gamequests, gamedoors, autopilot, fightbot, questbook, login
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
        self.breed_id = None; self._krg_event = threading.Event(); self._boosting = False
        self._map_changed_at = 0.0   # time of the last jow
        self._trace = None   # list of (time, 'S'|'C', message, hex) while a fight is being recorded (see _do_attack)
        self._icj_events = {}; self._dlg = None; self._done = collections.deque(maxlen=60)   # dialog being recorded; (time, quest, objective) accomplished lately
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
            self._krg_event.set(); self._maybe_auto_boost()
        elif t == gamestate.ISM:
            sl = gamestate.shortcut_spells(body)
            if sl: self.spell_ids = [sid for _slot, sid in sorted(sl)]
        if t == gamestate.KSC:
            self.char_id = gamestate.selection(body)[0]; self.breed_id = gamestate.breed_of(body)
        elif t == gamestate.JOW:
            mid = next((v for f, v in parse(body) if f == 1 and isinstance(v, int)), None)
            self._map_changed_at = time.time()
            with self._lock:
                self._left_map = self.map_id
                self.map_id, self.my_cell, self.actors, self.npcs, self.elements, self.groups = mid, None, {}, {}, {}, {}
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
            for q, (st, objs) in list(self.quests.items()): self._book_quest(q, st, objs)
            self.log("event", f"Quêtes en cours : {len(self.quests)}")
        elif t == login.ICJ:
            q = gamequests.quest_state(body)
            if q:
                ev = self._icj_events.get(q[0])
                if ev: ev.set()
                with self._lock: self.quests[q[0]] = (q[1], q[2])
                self._book_quest(q[0], q[1], q[2])
                name = (gamedata.quest_data("quests", q[0]) or {}).get("name") or q[0]
                self.log("event", f"Quête « {name} » mise à jour : " + " ; ".join(f"{gamequests.objective_text(o)}{'' if a else ' ✓'}" for o, a in q[2].items()))
        elif t == login.ICB:
            qid = _vi(body, 1)
            name = (gamedata.quest_data("quests", qid) or {}).get("name") or qid
            self.log("event", f"Quête modifiée : « {name} »")
            try:
                self._send(login.IBV, f_varint(3, qid) + f_varint(4, -1))   # same as the real client: the server answers with icj
                self._send(login.IBJ, f_varint(1, qid))                     # a quest that has just started is only loaded by an explicit state request
            except RuntimeError: pass
        elif t == login.IBG:
            oid, qid = _vi(body, 1), _vi(body, 2)
            self._done.append((time.time(), qid, oid))
            with self._lock:
                if qid in self.quests and oid in self.quests[qid][1]: self.quests[qid][1][oid] = False   # done (the next icj confirms)
            self.log("event", f"Objectif accompli : {gamequests.objective_text(oid)}")
        elif t == login.ILX:
            actor = _vi(body, 5)
            with self._lock: self.dialog = {"actor": actor, "text": None, "replies": []}
            self._dlg = {"npc": self.npcs.get(actor), "map": self.map_id, "path": [], "t": time.time(), "active": self._active_objectives()}
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
            d, self._dlg = self._dlg, None
            if d: threading.Timer(6.0, self._dlg_done, (d,)).start()   # the server confirms an objective shortly after the dialog ends
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
            if aid == self.char_id:
                first = self.my_cell is None; self.my_cell = cell
                if first: self._note_arrival(cell)
            tpl = gamequests.npc_template(entry) if aid < 0 else None
            if tpl: self.npcs[aid] = tpl
            elif aid < 0 and not self.in_fight:
                mons = fightbot.monster_group(entry)
                if mons: self.groups[aid] = mons

    def sync_quests(self, wait=3.0):
        """Ask the server for every known quest (ibj); the ones that do not answer with icj are finished: drop them.
        -> list of the quest ids that were dropped."""
        with self._lock: ids = list(self.quests)
        self._icj_events = {q: threading.Event() for q in ids}
        for q in ids:
            try: self._send(login.IBJ, f_varint(1, q))
            except RuntimeError: return []
        end = time.time() + wait
        while time.time() < end and not all(e.is_set() for e in self._icj_events.values()): time.sleep(0.1)
        gone = [q for q, e in self._icj_events.items() if not e.is_set()]
        self._icj_events = {}
        with self._lock:
            for q in gone: self.quests.pop(q, None)
        for q in gone: self.log("event", f"Quête terminée : « {(gamedata.quest_data('quests', q) or {}).get('name') or q} »")
        return gone

    def guide_state(self):
        """Compact description of the situation for the LLM guide (game texts only)."""
        import autopilot as A
        mv = self.mapview(); info = mv.get("info") or {}
        sides = [{"side": s, "vers": (f"{v['to']['subarea']} [{v['to']['x']}, {v['to']['y']}]" if v.get("to") else "?")}
                 for s, v in (mv.get("sides") or {}).items() if v.get("available")]
        objectives, targets = [], {}
        with self._lock: quests = {q: (st, dict(objs)) for q, (st, objs) in self.quests.items()}
        for qid, (_st, objs) in quests.items():
            name = (gamedata.quest_data("quests", qid) or {}).get("name")
            for oid, active in objs.items():
                if not active: continue
                o = gamedata.quest_data("objectives", oid) or {}; mp = gamedata.map_info(o["map"]) if o.get("map") else None
                objectives.append({"quête": name, "objectif": gamequests.objective_text(oid), "où": f"{mp['subarea']} [{mp['x']}, {mp['y']}]" if mp else None})
                if o.get("map") and (A.route(self.map_id, int(o["map"])) is not None or int(o["map"]) == self.map_id): targets[int(o["map"])] = objectives[-1]["où"]
        return {"carte": f"{info.get('subarea')} [{info.get('x')}, {info.get('y')}]" if info else None, "sorties_à_pied": sides,
                "pnj": [{"actor": n["actor"], "nom": n["name"]} for n in mv.get("npcs", [])],
                "éléments": [{"element": e["element"], "nom": e.get("name"), "mène_à": (f"{e['to']['subarea']} [{e['to']['x']}, {e['to']['y']}]" if e.get("to") else None)} for e in mv.get("elements", [])],
                "groupes": [{"actor": g["actor"], "monstres": [m["name"] for m in g["monsters"]]} for g in mv.get("groups", [])],
                "objectifs_actifs": objectives[:8], "cartes_atteignables_des_objectifs": [{"map_id": k, "où": v} for k, v in targets.items()],
                # validation helpers (not sent to the model)
                "npcs": [{"actor": n["actor"]} for n in mv.get("npcs", [])], "groups": [{"actor": g["actor"]} for g in mv.get("groups", []) if len(g["monsters"]) <= 3],
                "elements": [{"element": e["element"]} for e in mv.get("elements", [])], "sides": sides, "reachable_maps": list(targets)}

    # ---- capital points ----
    def boost_view(self):
        s = boost.load_settings(self.aid); f = boost.fields()
        return {"live": self.status == "connected", "points": self.stats.get(boost.POINTS), "breed": gamedata.get("breeds", self.breed_id) if self.breed_id else None,
                "auto": s["auto"], "weights": s["weights"],
                "stats": [{"id": c, "name": n, "value": self.stats.get(c, 0), "cost": boost.cost(self.breed_id, c, self.stats.get(c, 0)),
                           "verified": f[c][1]} for c, n in boost.STATS.items()]}

    def _maybe_auto_boost(self):
        """After every characteristics update: spend the points automatically if the player asked for it."""
        if self._boosting or self.in_fight or self.status != "connected" or not (self.stats.get(boost.POINTS) or 0) > 0: return
        if not boost.load_settings(self.aid)["auto"]: return
        threading.Timer(1.5, lambda: self.start_boost(auto=True)).start()

    def start_boost(self, weights=None, auto=False):
        if self.in_fight: return "combat en cours"
        return self.start_action("Répartir les points de caractéristiques", lambda: self._do_boost(weights))

    def _wait_krg(self, timeout=4.0):
        return self._krg_event.wait(timeout)

    def _do_boost(self, weights=None):
        """Spend the capital points following the weights. The first boost into a characteristic whose message field is not verified is a
        single one, checked against the server's answer (which characteristic really moved)."""
        self._boosting = True
        try:
            s = boost.load_settings(self.aid); weights = weights or s["weights"]
            points = self.stats.get(boost.POINTS) or 0
            if points <= 0: self.log("info", "Aucun point de caractéristiques à répartir"); return
            if not self.breed_id: raise RuntimeError("classe inconnue")
            values = {c: self.stats.get(c, 0) for c in boost.STATS}
            todo, used = boost.plan(points, values, self.breed_id, weights)
            if not todo: raise RuntimeError("répartition vide (poids nuls ou points insuffisants)")
            self.log("info", "Répartition prévue : " + ", ".join(f"{boost.STATS[c]} +{n}" for c, n in todo.items()) + f" ({used} points sur {points})")
            for cid, n in todo.items():
                field, verified = boost.fields()[cid]
                first = 1 if not verified else n
                for amount in ((first, n - first) if not verified and n > 1 else (n,)):
                    if amount <= 0: continue
                    before = dict(self.stats); self._krg_event.clear()
                    self._send(boost.KRC, f_varint(field, amount)); self.log("action", f"→ krc : {boost.STATS[cid]} +{amount} (champ {field})")
                    if not self._wait_krg(): raise RuntimeError("le serveur n'a pas confirmé la répartition (pas de krg)")
                    moved = {c: self.stats.get(c, 0) - before.get(c, 0) for c in boost.STATS if self.stats.get(c, 0) != before.get(c, 0)}
                    spent = (before.get(boost.POINTS) or 0) - (self.stats.get(boost.POINTS) or 0)
                    if cid in moved and moved[cid] >= amount:
                        if not verified: boost.verify(cid, field); verified = True; self.log("event", f"Champ {field} = {boost.STATS[cid]} confirmé")
                    elif moved:
                        real = max(moved, key=lambda c: moved[c])
                        boost.verify(real, field)
                        raise RuntimeError(f"calibrage : le champ {field} augmente {boost.STATS[real]} (et non {boost.STATS[cid]}) : noté, relance la répartition")
                    else: raise RuntimeError(f"aucun effet sur {boost.STATS[cid]} (points dépensés : {spent})")
                    time.sleep(0.4)
            self.log("event", f"Points répartis ; restants : {self.stats.get(boost.POINTS)}")
        finally:
            self._boosting = False

    def _active_objectives(self):
        with self._lock: return [o for _q, (_st, objs) in self.quests.items() for o, active in objs.items() if active]

    def _book_quest(self, qid, step, objs):
        try:
            q = gamedata.quest_data("quests", qid) or {}; st = gamedata.quest_data("steps", step) or {}
            questbook.observe(qid, step, list(objs), q.get("name"), st.get("name"), {o: gamequests.objective_text(o) for o in objs})
        except Exception: pass   # the book is a convenience: never break the session

    def _completed_since(self, t0):
        return [(q, o) for t, q, o in list(self._done) if t >= t0]

    def _dlg_done(self, d):
        try: questbook.record_dialog(d["npc"], d["map"], d["path"], self._completed_since(d["t"]), d["active"])
        except Exception: pass

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
                                   "cells": gamemaps.exit_cells(m, side), "to": (gamedata.map_info(near.get(side) or m["neighbours"].get(side)) if (near.get(side) or m["neighbours"].get(side)) else None)}
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
    def trace(self, kind, name, body):
        if self._trace is not None: self._trace.append((round(time.time(), 3), kind, name, body.hex()))

    def _send(self, name, body=b""):
        if self.conn is None: raise RuntimeError("non connecté")
        self.trace("C", name, body)
        self.conn.send(request(name, body))

    def _walk(self, path):
        """Send jok for `path`, wait for the server's jnz and the walking time, then confirm with jny."""
        keys = gamemaps.movement_keys(path)
        self._moved.clear(); self._mapchanged.clear()
        for attempt in range(2):     # after a refused request (joc, itz...) the server swallows the first move: send it once more
            self._send(login.JOK, f_bytes(1, b"".join(enc_varint(k) for k in keys)) + f_varint(2, self.map_id))
            self.log("action", f"→ jok : {len(path) - 1} cases, de {path[0]} à {path[-1]}" + (" (2e envoi)" if attempt else ""))
            if self._moved.wait(3): break
        else: raise RuntimeError("le serveur n'a pas confirmé le déplacement (jnz)")
        end = time.time() + gamemaps.duration(path)
        while time.time() < end:
            if self._stop.is_set(): raise RuntimeError("déconnecté")
            if self._mapchanged.is_set(): return   # the last cell was a trigger: the server already loaded the next map (no jny)
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
        """Leave the map through `side`: walk to an exit cell, then joc (some borders change the map by themselves on arrival).
        If the server refuses, try the next exit cells; the cell that worked is remembered (knowledge/exit_cells.json) and tried first."""
        start_map = self.map_id
        m = self.mapdata(); target = (m or {}).get("neighbours", {}).get(side)
        if not m or not target: raise RuntimeError(f"pas de carte voisine ({side})")
        learned = knowledge.load("exit_cells.json", {}).get(f"{start_map}:{side}", [])
        cands = []
        for c in gamemaps.exit_cells(m, side):
            path, _ = self.plan(c)
            if path: cands.append((0 if c in learned else 1, len(path), c, path))
        if not cands: raise RuntimeError(f"aucune case de sortie atteignable ({side})")
        cands.sort(key=lambda x: x[:3])
        for n, (_pref, _len, cell, path) in enumerate(cands[:4]):
            if self.my_cell != cell: path, _ = self.plan(cell)
            if not path: continue
            if len(path) > 1: self._walk(path)
            if self.map_id != start_map and self._mapchanged.is_set():   # some borders change the map as soon as the exit cell is reached
                self.log("info", f"la carte a changé en marchant sur la case de sortie {cell}"); self._remember_exit(start_map, side, cell); time.sleep(1.0); return
            self._mapchanged.clear()
            self._send(login.JOC, f_varint(2, target)); self.log("action", f"→ joc : carte voisine {side} ({target}) depuis la case {cell}")
            if self._mapchanged.wait(6):
                self._remember_exit(start_map, side, cell); time.sleep(1.0); return
            self.log("warn", f"la case de sortie {cell} est refusée par le serveur" + (" : essai d'une autre case" if n < min(len(cands), 4) - 1 else ""))
        raise RuntimeError("le serveur n'a pas changé de carte (jow) pour aucune case de sortie essayée")

    def _remember_exit(self, map_id, side, cell):
        with knowledge._lock:
            d = knowledge.load("exit_cells.json", {}); lst = d.setdefault(f"{map_id}:{side}", [])
            if cell not in lst: lst.insert(0, cell); knowledge.save("exit_cells.json", d)

    def go(self, to):
        return self.start_action(f"Aller à la case {to}", lambda: self._do_go(to))

    def change_map(self, side):
        return self.start_action(f"Changer de carte ({side})", lambda: self._do_change_map(side))

    def _do_use(self, element, stand=None, until=None):
        """Walk next to an interactive element, use it (itz) and learn where it leads if the map changes."""
        e = self.elements.get(element)
        if not e: raise RuntimeError("élément introuvable sur la carte")
        frm = self.map_id
        known = next((d for d in gamedoors.from_map(frm) if d["element"] == element), None)
        if stand is not None: known = dict(known or {}, stand=stand)   # explicit cell to use it from (probing an unknown door)
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
        self._send(login.ITZ, (f_varint(1, e["skill"]) if e["skill"] else b"") + f_varint(3, element))   # protobuf omits a zero skill id
        self.log("action", f"→ itz : élément {element} (compétence {e['skill']})")
        if until:      # quest objective ("read the book", "use ..."): done when until() says so; a map change is a door and learned below
            t0 = time.time()
            while time.time() - t0 < 4:
                if until(): return True
                if self._mapchanged.is_set(): break
                time.sleep(0.2)
            else: return False
        if self._mapchanged.wait(8):
            time.sleep(1.0)
            new = gamedoors.record(frm, e["skill"], element, e["cell"], self.map_id, known.get("stand") if known else None)
            info = gamedata.map_info(self.map_id)
            self.log("event", f"Passage {'appris' if new else 'connu'} : {frm} → {self.map_id}" + (f" ({info['subarea']} [{info['x']}, {info['y']}])" if info else ""))
        else:
            raise RuntimeError("aucun changement de carte après l'utilisation de l'élément (mauvaise case d'utilisation ?)")

    PROBE_FILE = "probed_doors.json"

    def probe_doors(self, max_cells=8):
        """Door discovery on the current map: try every unknown interactive element (door skills first) from the cells where its use-cell is
        predicted (gamemaps.stand_candidates). Stops at the first one that changes the map (it is recorded by _do_use). Cells already tried
        are remembered in knowledge/probed_doors.json. -> True if a passage was found."""
        mid = self.map_id; m = self.mapdata()
        if not m or mid is None: return False
        pos = gamemaps.element_positions(mid)
        known = {d["element"] for d in gamedoors.from_map(mid) if d.get("element")}
        skills = {d.get("skill") for lst in gamedoors.load().values() for d in lst if d.get("skill")}
        with self._lock: els = dict(self.elements)
        # only skills already seen on real doors: other elements are resources (a harvest locks the character until it ends), workshops, zaaps...
        order = sorted((e for e in els if e not in known and e in pos and els[e].get("skill") in skills))
        for e in order:
            for c in gamemaps.stand_candidates(m, pos[e], max_cells):
                tried = knowledge.load(self.PROBE_FILE, {}).get(str(mid), {}).get(str(e), [])
                if c in tried: continue
                with knowledge._lock:
                    d = knowledge.load(self.PROBE_FILE, {}); d.setdefault(str(mid), {}).setdefault(str(e), []).append(c); knowledge.save(self.PROBE_FILE, d)
                self.log("info", f"Découverte de porte : élément {e} (compétence {els[e]['skill']}) depuis la case {c}")
                try: self._do_use(e, c)
                except RuntimeError as err:
                    self.log("info", f"  sans effet : {err}"); self._unlock_after_use(c); continue
                return True
        return False

    def _unlock_after_use(self, near):
        """A wrong use leaves the server waiting (ity): the first move afterwards is swallowed. Send a throw-away step, then give it a second."""
        for _ in range(2):
            try:
                m = self.mapdata(); x, y = gamemaps.to_xy(self.my_cell)
                n = next((c for dx, dy in gamemaps.DIRS for c in [gamemaps.from_xy(x + dx, y + dy)] if c is not None and gamemaps.walkable(m, c)), None)
                if n is not None: self._walk([self.my_cell, n]); break
            except RuntimeError: time.sleep(1.2)
        time.sleep(0.8)

    def use_recorded(self, oid, until):
        """Replay the element uses learned from a real session for objective `oid` (knowledge/element_uses.json). -> True when done."""
        rec = knowledge.load("element_uses.json", {}).get(str(oid))
        if not rec or rec.get("map") != self.map_id: return False
        for st in rec["steps"]:
            self.log("info", f"Objectif : élément {st['element']} depuis la case {st['stand']} (appris)")
            try: done = self._do_use(st["element"], st["stand"], until)
            except RuntimeError as err: self.log("info", f"  sans effet : {err}"); return False
            if done: return True
            if st.get("kiq"): self._send("kiq"); time.sleep(1.0)
        return bool(until())

    def use_for_objective(self, words, until, max_cells=6):
        """Quest objective that wants an interactive element used (a book to read...), no element id given: try the elements of the map
        whose model name matches one of `words` first, then the others, each from the cells where its use-cell is predicted, until the
        objective is done. Door-like results (map change) are learned as usual. -> True when `until()` became true."""
        m = self.mapdata(); mid = self.map_id
        if not m: return False
        pos = gamemaps.element_positions(mid)
        known = {d["element"] for d in gamedoors.from_map(mid) if d.get("element")}
        with self._lock: els = {k: dict(v) for k, v in self.elements.items()}
        def name(e): return (gamedata.get("interactives", els[e]["interactive"]) or "") if els[e].get("interactive") is not None else ""
        # a named element that matches no word of the objective is a resource / workshop / zaap: never touched (harvesting locks the character)
        order = sorted((e for e in els if e not in known and els[e].get("skill") and (not name(e) or any(w in name(e).lower() for w in words))),
                       key=lambda e: (not name(e), e))
        for e in order:
            cells = [None] + (gamemaps.stand_candidates(m, pos[e], max_cells, 260) if e in pos else [])
            for c in cells:
                self.log("info", f"Objectif : utilisation de l'élément {e} « {name(e) or '?'} » depuis la case {c if c is not None else 'actuelle'}")
                try:
                    if self._do_use(e, c, until): return True
                except RuntimeError as err: self.log("info", f"  sans effet : {err}")
                if self.map_id != mid: return False      # it was a door: the caller travels back
        return False

    def _note_arrival(self, cell):
        """First position on a new map. An interior (no border exit) is left next to where we arrive: remember that cell as a guess of the way back."""
        left = getattr(self, "_left_map", None); mid = self.map_id
        if not left or not mid or left == mid: return
        def work():
            try:
                m = gamemaps.load(mid)
                if m and not any(gamemaps.exit_cells(m, s) for s in ("right", "bottom", "left", "top")):
                    if gamedoors.record_guess(mid, cell, left): self.log("info", f"Intérieur {mid} : sortie probable près de la case d'arrivée {cell}")
            except Exception: pass
        threading.Thread(target=work, daemon=True).start()

    def _guess_cells(self, cell, radius=3):
        """Walkable cells around `cell`, nearest first (the cell itself first)."""
        m = self.mapdata()
        if not m: return [cell]
        x0, y0 = gamemaps.to_xy(cell); out = []
        for c in range(gamemaps.CELLS):
            if not gamemaps.walkable(m, c): continue
            x, y = gamemaps.to_xy(c); d = max(abs(x - x0), abs(y - y0))
            if d <= radius: out.append((d, c))
        return [c for _d, c in sorted(out)]

    def _do_step(self, cell, guess=False):
        """Walk onto a trigger cell: the server loads the next map by itself (learned passage, see gamedoors.record_step).
        A guessed cell (interior exit deduced from the arrival cell) is tried with its neighbours; the one that works is recorded."""
        frm = self.map_id; prev = frm
        tries = self._guess_cells(cell) if guess else [cell]
        for n, c in enumerate(tries[:14]):
            self._mapchanged.clear()
            if self.my_cell != c:
                try: self._do_go(c)
                except RuntimeError: continue
            if self._mapchanged.wait(8 if not guess else 3.5):
                to = self.map_id
                if guess: gamedoors.record_step(frm, c, to); self.log("event", f"Sortie de {frm} trouvée : case {c} (supposée {cell})")
                time.sleep(1.0); return
        raise RuntimeError("aucun changement de carte en marchant sur la case-déclencheur" + (" (essai des cases voisines de l'arrivée)" if guess else ""))

    def use_element(self, element, stand=None):
        return self.start_action(f"Utiliser l'élément {element}", lambda: self._do_use(element, stand))

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
        t_fight = time.time()
        self._trace = []   # record every message of this fight for a post-mortem (dofus_net/fights/*.jsonl)
        try: self.fight.play()
        finally:
            self._dump_trace(mons)
            threading.Timer(6.0, lambda: questbook.record_fight(self.map_id, mons, self._completed_since(t_fight))).start()

    def _dump_trace(self, monsters):
        tr, self._trace = self._trace, None
        if not tr: return
        try:
            d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fights"); os.makedirs(d, exist_ok=True)
            names = "-".join((gamedata.quest_data("monsters", m) or str(m)).replace(" ", "_") for m in monsters)[:60]
            path = os.path.join(d, time.strftime("%Y%m%d-%H%M%S") + f"_{names}.jsonl")
            head = {"map": self.map_id, "monsters": monsters, "my_cell": self.my_cell, "stats": self.stats, "spells": self.spell_ids}
            with open(path, "w", encoding="utf8") as f:
                f.write(json.dumps(head) + "\n")
                for e in tr: f.write(json.dumps(e) + "\n")
            self.log("info", f"Combat enregistré : {os.path.basename(path)}")
        except Exception as e: self.log("warn", f"enregistrement du combat impossible : {e}")

    def _do_scripted_fight(self, since):
        """A dialog may start a fight by itself (boss, quest fight): the server then loads the fight map right after the dialog.
        -> True if a fight was played."""
        end = time.time() + 6
        while self._map_changed_at < since and time.time() < end: time.sleep(0.2)
        if self._map_changed_at < since: return False
        time.sleep(2.5)   # like the real client: wait for the fight map before the placement phase
        self._trace = []
        try: self.fight.play()
        except RuntimeError as e:
            self.log("warn", f"pas de combat après le dialogue : {e}"); return False
        finally: self._dump_trace([])
        return True

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
            if best is None:      # NPC on an island (counter, stage...): get as close as the walkable floor allows and ask from there
                near = sorted((max(abs(a - x), abs(b - y)), c) for c in range(gamemaps.CELLS) if gamemaps.walkable(m, c) for a, b in [gamemaps.to_xy(c)])
                for d, c in near:
                    if d > 4: break
                    path, _ = self.plan(c)
                    if path: best = path; self.log("info", f"PNJ isolé : je m'approche à {d} case(s) (case {c})"); break
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

    def close_dialog(self):
        """A dialog that stays open without replies (a one-line NPC) blocks every move: close it like the real client does (kiq)."""
        if not self.dialog: return
        try: self._send("kiq")
        except RuntimeError: return
        for _ in range(10):
            if not self.dialog: return
            time.sleep(0.2)
        with self._lock: self.dialog = None

    def _do_reply(self, rid):
        if not self.dialog or rid not in [r["id"] for r in self.dialog.get("replies", [])]: raise RuntimeError("réponse indisponible")
        self._dialog_event.clear()
        self._send(login.IKY, f_varint(1, rid)); self.log("action", f"→ iky : réponse {rid}")
        if self._dlg is not None: self._dlg["path"].append(rid)
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
                self.recent.append((time.time(), t, body)); self.trace("S", t, body)
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
