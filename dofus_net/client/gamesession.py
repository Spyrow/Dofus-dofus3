"""Persistent game session per UI account: refresh the HAAPI key, get a game token, log in, enter the character, then stay
connected (keep-alive like the real client) while decoding the pushed state (gamestate.py) into the account's snapshot.

Status: disconnected | connecting | connected | error. Each session keeps a journal (connection steps, actions, events)
and a chat buffer, both read by the web UI's Console tab. Needs the Shield certificate (shield.py) and the real game
closed (a second session on the same account kicks the first, in both directions).

Chat: the chat messages are NOT decoded yet (none in the captures so far): `Session.chat` stays empty until gamestate
learns the message; see add_chat()."""
import collections, json, os, threading, time
import boost, knowledge, accounts, shield, gamestate, gamedata, gamemaps, gamequests, gamedoors, gamejobs, gameachievements, autopilot, fightbot, questbook, login
from wire import parse, dec_varint, enc_varint, f_bytes, f_varint, request
from haapi import create_game_token, GAME_IDS

QUEST_MSGS = {login.ICA, login.ICJ, login.ICB, login.IBG, login.ILX, login.ILF, login.KIS}
FIGHT_MSGS = {login.JUO, login.JWJ, login.JVJ, login.JTE, login.JUG, login.JSY, login.JTK, login.JSQ, login.JXI, login.JWP}   # JWP was missing: the fight engine never saw the end of a fight
JOB_MSGS = gamejobs.MESSAGES | {gamestate.IRL, gamestate.IQX}   # harvest / craft / inventory / job experience (see gamejobs.py)
KEEP = {login.JNJ, login.JNS, login.JNZ, login.JXI, login.JWP} | QUEST_MSGS | FIGHT_MSGS | JOB_MSGS | {gamestate.KSC, gamestate.KRG, gamestate.IRL, gamestate.IQX, gamestate.IPE, gamestate.ISM, gamestate.JOW}
HEARTBEAT = 5          # seconds, same cadence as the real client's kne
CONNECT_TIMEOUT = 45   # seconds without any character data before giving up
DOOR_TYPE = 184        # jnj element type of the doors / stairs seen in the captures (resources: 45 wheat, 68 nettle, 124 fish; 114 zaap)
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
        self.groups = {}; self.stats = {}; self.base = {}; self.spell_ids = []; self.fight = fightbot.Fight(self)   # monster groups {actor: [monster ids]}
        self.elements = {}   # interactive elements of the current map (from jnj)
        self.in_fight = False; self.auto = autopilot.Autopilot(self)
        self.breed_id = None; self._krg_event = threading.Event(); self._boosting = False
        self._map_changed_at = 0.0   # time of the last jow
        self._trace = None   # list of (time, 'S'|'C', message, hex) while a fight is being recorded (see _do_attack)
        self._icj_events = {}; self._dlg = None; self._done = collections.deque(maxlen=60)   # dialog being recorded; (time, quest, objective) accomplished lately
        self.finished_quests = set(); self.npcs = {}; self.quests = {}; self.dialog = None   # actor id -> template id; quest id -> (step, objectives); open dialog
        self._dialog_event = threading.Event(); self.recent = collections.deque(maxlen=200)   # (time, message type) of everything received
        self._act = threading.Lock(); self.busy = None; self._moved = threading.Event(); self._mapchanged = threading.Event()
        self._stop = threading.Event(); self._thread = None; self._lock = threading.RLock()
        self.equipped = set()   # item ids worn (login inventory, then iqn)
        self._equip_evt = threading.Event(); self._equip_want = None; self.achievements = {}; self._claimed = []; self._claimed_evt = threading.Event()
        self.arrival_cell = None   # first cell on the current map: the door we came through is next to it
        self.arrival_inside = False   # True when we came in through a door / trigger cell (not across a border): only then is the arrival cell next to a door
        self._last = {}            # what the client last sent: {"jok": (time, map, last cell), "itz": (time, map, skill, element, stand), "joc": time, "hms": time}
        self.inv = {}; self.jobxp = {}; self.unavailable = {}   # stack uid -> [item id, quantity]; job id -> {xp, floor, next}; element id -> time until which it gives nothing
        self._harvest_started, self._harvest_end = threading.Event(), threading.Event()
        self._craft_open, self._table_event, self._crafted_event, self._craft_closed = (threading.Event() for _ in range(4)); self._craft_skill = None

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
        if t in JOB_MSGS: self._on_job_message(t, body); return
        if t in FIGHT_MSGS:
            self.fight.on_message(t, body)
            self.in_fight = t != login.JWP
        if t == gamestate.KRG:
            try: self.stats = gamestate.characteristics(body); self.base.update(gamestate.base_characteristics(body))
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
                self.arrival_inside = False
                try: self._learn_passage(self.map_id, mid)
                except Exception as e: self.log("warn", f"apprentissage du passage impossible : {e}")
                self.map_id, self.my_cell, self.actors, self.npcs, self.elements, self.groups = mid, None, {}, {}, {}, {}
                self.arrival_cell = None
            self._mapchanged.set()
        elif t in (login.JNJ, login.JNS):      # jnj {3: [entry]} / jns {1: entry}; entry = {1: actor id, 2: {3: cell, 4: direction}, 3: ...}
            for f, v in parse(body):
                if f != (3 if t == login.JNJ else 1) or not isinstance(v, bytes): continue
                self._register_actor(v)
            if t == login.JNJ:
                with self._lock: self.elements.update(gamedoors.elements(body))
        elif t == login.JXI:
            self.log("event", "Combat engagé")
        elif t == login.JWP:
            self.log("event", "Combat terminé")
        elif t == login.ICA:
            with self._lock: self.finished_quests = gamequests.finished_quests(body)
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
                if first: self.arrival_cell = cell; self._note_arrival(cell)
            tpl = gamequests.npc_template(entry) if aid < 0 else None
            if tpl: self.npcs[aid] = tpl
            elif aid < 0 and not self.in_fight:
                mons = fightbot.monster_group(entry)
                if mons: self.groups[aid] = mons

    # ---- jobs: inventory, harvest, craft (messages: gamejobs.py) ----
    def have(self, item_id):
        with self._lock: return sum(q for g, q in self.inv.values() if g == item_id)

    def _on_job_message(self, t, body):
        if t == gamestate.IRL:     # the login inventory carries the equipment; the lists sent when a workshop opens do not
            items = gamestate.inventory_items(body)
            if any(it["position"] not in (None, gamestate.NOT_EQUIPPED) for it in items):
                with self._lock:
                    self.inv = {it["uid"]: [it["gid"], it["qty"]] for it in items if it["uid"] is not None}
                    self.equipped = {it["gid"] for it in items if it["position"] not in (None, gamestate.NOT_EQUIPPED)}
        elif t == gamestate.IQX:
            with self._lock:
                for it in gamestate.inventory_items(body, True):
                    if it["uid"] is not None: self.inv[it["uid"]] = [it["gid"], it["qty"]]
        elif t == gamejobs.IRX:
            sq = gamejobs.stack_quantity(body)
            with self._lock:
                if sq and sq[0] in self.inv: self.inv[sq[0]][1] = sq[1]
        elif t == gamejobs.KCO:
            it = gamejobs.crafted(body)
            if it and it["uid"] is not None:
                with self._lock: self.inv[it["uid"]] = [it["gid"], it["qty"]]
            self._crafted_event.set()
            self.log("event", f"Fabriqué : {gamejobs.item_name(it['gid']) if it else '?'}")
        elif t == gamejobs.ITW:
            e = gamejobs._sub(body, 1); el, cell, state = gamejobs._int(e, 3), gamejobs._int(e, 4), gamejobs._int(e, 5)
            with self._lock:
                if el in self.elements and cell is not None: self.elements[el]["cell"] = cell
            if cell is not None and el is not None and self.map_id:
                with knowledge._lock:
                    d = knowledge.load("element_cells.json", {}); m = d.setdefault(str(self.map_id), {})
                    if m.get(str(el)) != cell: m[str(el)] = cell; knowledge.save("element_cells.json", d)
            if state in (1, 2) and el is not None: self.unavailable[el] = time.time() + 45   # being harvested / growing back
        elif t == gamejobs.ITV:
            if gamejobs._int(body, 4) == self.char_id: self._harvest_started.set()
        elif t == gamejobs.ITT: self._harvest_end.set()
        elif t == gamejobs.ISZ:
            gid, qty = gamejobs.loot(body)
            self.log("event", f"Récolte : +{qty} {gamejobs.item_name(gid) or gid}")
        elif t == gamejobs.IPZ:
            xp = gamejobs.job_xp(body)
            if xp:
                with self._lock: old = self.jobxp.get(xp["job"]); self.jobxp[xp["job"]] = xp
                name = gamedata.get("jobs", xp["job"]) or f"métier {xp['job']}"
                lvl = gamejobs.level_of(xp["floor"], 0)
                self.log("event", f"{name} : {xp['xp']} / {xp['next']} XP" + (f" (niveau {lvl})" if lvl else "") +
                         (" — niveau supérieur !" if old and xp["floor"] > old["floor"] else ""))
        elif t == gamejobs.IQN:      # iqn {1: item uid, 3: position}: worn (1 = weapon...) or back in the bag (63)
            uid, pos = _vi(body, 1), _vi(body, 3)
            with self._lock:
                gid = (self.inv.get(uid) or [None])[0]
                if gid is not None:
                    if pos in (None, gamestate.NOT_EQUIPPED): self.equipped.discard(gid)
                    else: self.equipped.add(gid)
            if self._equip_want == (uid, pos): self._equip_evt.set()
        elif t == "mdp":
            self.achievements = {a: pending for a, pending in gameachievements.listing(body)}
            self.log("event", f"Succès : {len(self.achievements)} débloqués, {sum(self.achievements.values())} avec une récompense à réclamer")
        elif t == "mdj":
            a = _vi(body, 1); self._claimed.append(a); self._claimed_evt.set()
            self.log("event", f"Succès validé : « {gameachievements.name(a)} »")
        elif t == gamejobs.JYZ: self._craft_skill = _vi(body, 1); self._craft_open.set()
        elif t == gamejobs.KCH: self._table_event.set()
        elif t == gamejobs.KBR: self._craft_closed.set()

    def _walk_next_to(self, cell):
        """Stand on a cell at distance 1 of an element (skill range 1 = a direct neighbour in the game grid: (+-1, 0) or (0, +-1); the real client
        stood there for the nettle: element (8, -6), character (8, -7))."""
        x, y = gamemaps.to_xy(cell)
        adjacent = {gamemaps.from_xy(x + dx, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))} - {None}
        if self.my_cell in adjacent: return
        best = None
        for c in adjacent:
            path, _ = self.plan(c)
            if path and (best is None or len(path) < len(best)): best = path
        if best is None: raise RuntimeError("aucune case accessible à côté de l'élément")
        if len(best) > 1: self._walk(best)

    def _do_harvest(self, element):
        """Walk next to a resource and harvest it once (itz): the server answers itv at once and itt about 3 s later. Loot and experience follow."""
        e = self.elements.get(element)
        if not e or not e.get("skill"): raise RuntimeError("ressource introuvable sur la carte")
        mid = self.map_id; known = True
        cell = e.get("cell")
        if cell is None: cell = knowledge.load("element_cells.json", {}).get(str(mid), {}).get(str(element))      # told by the server on an earlier harvest
        if cell is None: cell = gamemaps.element_cells(mid).get(element)
        if cell is None:        # trees have no cell in the map data: estimate it from the sprite position (exact for the animated elements measured)
            pos = gamemaps.element_positions(mid).get(element)
            if pos is None: raise RuntimeError("case de la ressource inconnue")
            cell = min(range(gamemaps.CELLS), key=lambda c: (gamemaps.cell_center(c)[0] - pos[0] - 623.5) ** 2 + (gamemaps.cell_center(c)[1] - 505.25 + pos[1]) ** 2)
            known = False
        x, y = gamemaps.to_xy(cell)
        ring = [gamemaps.from_xy(x + dx, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))]      # direct neighbours, then the diagonals
        stands = []
        for i, c in enumerate(ring):
            if c is None: continue
            path, _ = self.plan(c)
            if path: stands.append((i >= 4, len(path), c))
        stands = [c for _d, _l, c in sorted(stands)][:5 if known else 6]
        if not stands: raise RuntimeError("aucune case accessible à côté de l'élément")
        name = gamedata.get("interactives", e.get("interactive")) or "?"; t0 = time.time()
        for n, stand in enumerate(stands):
            if self.my_cell != stand:
                path, _ = self.plan(stand)
                if path and len(path) > 1: self._walk(path)
            self._harvest_started.clear(); self._harvest_end.clear(); t0 = time.time()
            self._send(login.ITZ, f_varint(1, e["skill"]) + f_varint(3, element))
            self.log("action", f"→ itz : récolte de l'élément {element} « {name} » (compétence {e['skill']}) depuis la case {self.my_cell}, ressource en {cell}" + ("" if known else " (estimée)"))
            if self._harvest_started.wait(3): break
            self._unlock_after_use(self.my_cell)
        else:
            self.unavailable[element] = time.time() + 60
            got = [f"{t}={b.hex()[:40]}" for ts, t, b in list(self.recent) if ts >= t0 and t not in ("knb", "jnz", None)]
            raise RuntimeError(f"la ressource ne répond depuis aucune des {len(stands)} cases essayées ; reçu : " + (", ".join(got) or "rien"))
        if not self._harvest_end.wait(15): raise RuntimeError("fin de récolte non reçue")
        time.sleep(0.8)   # the loot (isz / irx) and the experience (ipz) arrive right after itt

    def harvest(self, element):
        return self.start_action(f"Récolter l'élément {element}", lambda: self._do_harvest(element))

    def _do_equip(self, gid, position=1):
        """Wear an item: ito {1: 1, 2: uid, 3: position} (position 1 = the weapon slot, where the hunter's knife goes; the weapon already there goes
        back to the bag by itself). The server confirms with iqn {1: uid, 3: position}."""
        with self._lock: uid = next((u for u, (g, _q) in self.inv.items() if g == gid), None)
        if uid is None: raise RuntimeError(f"« {gamejobs.item_name(gid)} » n'est pas dans l'inventaire")
        if position == 1 and gid in self.equipped: self.log("info", f"« {gamejobs.item_name(gid)} » est déjà équipé"); return      # the server says nothing then
        self._equip_evt.clear(); self._equip_want = (uid, position)
        self._send(gamejobs.ITO, f_varint(1, 1) + f_varint(2, uid) + f_varint(3, position))
        self.log("action", f"→ ito : équiper « {gamejobs.item_name(gid)} » (objet {uid}, emplacement {position})")
        if not self._equip_evt.wait(4): raise RuntimeError("l'équipement n'a pas été confirmé (iqn)")
        time.sleep(0.6)

    def equip(self, gid):
        return self.start_action(f"Équiper l'objet {gid}", lambda: self._do_equip(int(gid)))

    def claim_achievements(self, wait=4.0):
        """Validate the achievements: mdw {1: -1} claims every reward waiting; the server answers mdj per achievement. -> number claimed."""
        if self.status != "connected": return 0
        self._claimed = []; self._claimed_evt.clear()
        self._send(gameachievements.MDW, f_varint(1, (1 << 64) - 1))      # -1 as a protobuf int64
        self.log("action", "→ mdw : réclamer les récompenses des succès")
        self._claimed_evt.wait(wait); time.sleep(1.5)
        n = len(self._claimed)
        self.log("info", f"Succès : {n} validé(s)" if n else "Succès : rien à réclamer")
        return n

    def claim_achievements_later(self, delay=0.0):
        """Same, in the background (after a finished quest, or once connected)."""
        def run():
            time.sleep(delay)
            try: self.claim_achievements()
            except Exception as e: self.log("warn", f"succès : {e}")
        threading.Thread(target=run, daemon=True).start()

    def workshop_elements(self, job, skill=None):
        """Elements of the current map that are workshops for a recipe: the element type is the recipe's skill (Four = 27 for the bread),
        then the workshops named after the job."""
        models = gamejobs.workshop_models(job)
        with self._lock: els = {k: dict(v) for k, v in self.elements.items()}
        return sorted(k for k, v in els.items() if v.get("skill") and skill is not None and v.get("type") == skill) +                sorted(k for k, v in els.items() if v.get("skill") and v.get("interactive") in models and v.get("type") != skill)

    def _do_craft(self, result_id, count=1, element=None):
        """At a workshop of the right job (this map): open it (itz from the cell where the workshop opens), pick the recipe (kar: the server puts
        the ingredients on the table), craft (kba), then close the interface (kiq, which the quest objective asks for)."""
        r = gamejobs.recipe(result_id)
        if not r: raise RuntimeError("recette inconnue")
        mid = self.map_id; m = self.mapdata()
        order = [element] if element else self.workshop_elements(r["job"], r["skill"])
        if not order: raise RuntimeError(f"aucun atelier de {gamedata.get('jobs', r['job']) or 'ce métier'} sur la carte")
        pos = gamemaps.element_positions(mid)
        opened = None
        for el in order:
            skill = self.elements[el]["skill"]
            learned = gamejobs.stand(mid, el)
            cells = ([learned] if learned is not None else []) + [c for c in (gamemaps.stand_candidates(m, pos[el], 10, 260) if el in pos and m else []) if c != learned]
            for c in cells or [None]:
                if c is not None and self.my_cell != c:
                    try: self._do_go(c)
                    except RuntimeError: continue
                self._craft_open.clear(); self._craft_skill = None
                self._send(login.ITZ, f_varint(1, skill) + f_varint(3, el))
                self.log("action", f"→ itz : atelier {el} depuis la case {self.my_cell}")
                if self._craft_open.wait(3): opened = el; gamejobs.remember_stand(mid, el, self.my_cell); break
                self._unlock_after_use(self.my_cell)
            if opened: break
        if not opened: raise RuntimeError("l'atelier ne s'ouvre depuis aucune case essayée")
        try:
            if self._craft_skill != r["skill"]: raise RuntimeError(f"cet atelier fabrique autre chose (compétence {self._craft_skill}, attendue {r['skill']})")
            name = gamejobs.item_name(result_id)
            for i in range(count):
                self._table_event.clear(); self._crafted_event.clear()
                self._send(gamejobs.KAR, f_varint(1, result_id))
                if not self._table_event.wait(3): raise RuntimeError(f"ingrédients de « {name} » non acceptés (il en manque ?)")
                time.sleep(0.8)
                self._send(gamejobs.KBA, f_varint(1, 1) + f_varint(2, 1)); self.log("action", f"→ kba : fabriquer {name} ({i + 1}/{count})")
                if not self._crafted_event.wait(5): raise RuntimeError("fabrication non confirmée")
                time.sleep(0.8)
        finally:
            self._craft_closed.clear()
            try:
                self._send(gamejobs.KIQ); self._send(gamejobs.KIQ)
                self._craft_closed.wait(3)
            except RuntimeError: pass

    def craft(self, result_id, count=1):
        return self.start_action(f"Fabriquer {gamejobs.item_name(result_id) or result_id}", lambda: self._do_craft(int(result_id), int(count)))

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
        if gone: self.claim_achievements_later(3.0)      # a finished quest often unlocks achievements
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
        return {"live": self.status == "connected", "points": self.base.get(boost.POINTS), "breed": gamedata.get("breeds", self.breed_id) if self.breed_id else None,
                "auto": s["auto"], "weights": s["weights"],
                "stats": [{"id": c, "name": n, "value": self.base.get(c, 0), "cost": boost.cost(self.breed_id, c, self.base.get(c, 0)),
                           "verified": f[c][1]} for c, n in boost.STATS.items()]}

    def _maybe_auto_boost(self):
        """After every characteristics update: spend the points automatically if the player asked for it."""
        if self._boosting or self.in_fight or self.status != "connected" or not (self.base.get(boost.POINTS) or 0) > 0: return
        if not boost.load_settings(self.aid)["auto"]: return
        threading.Timer(1.5, lambda: self.start_boost(auto=True)).start()

    def start_boost(self, weights=None, auto=False):
        if self.in_fight: return "combat en cours"
        return self.start_action("Répartir les points de caractéristiques", lambda: self._do_boost(weights, auto))

    def _wait_krg(self, timeout=4.0):
        return self._krg_event.wait(timeout)

    def _do_boost(self, weights=None, auto=False):
        """Spend the capital points following the weights. The first boost into a characteristic whose message field is not verified is a
        single one, checked against the server's answer (which characteristic really moved)."""
        self._boosting = True
        try:
            s = boost.load_settings(self.aid); weights = weights or s["weights"]
            points = self.base.get(boost.POINTS) or 0
            if points <= 0: self.log("info", "Aucun point de caractéristiques à répartir"); return
            if not self.breed_id: raise RuntimeError("classe inconnue")
            values = {c: self.base.get(c, 0) for c in boost.STATS}
            todo, used = boost.plan(points, values, self.breed_id, weights)
            if not todo: raise RuntimeError("répartition vide (poids nuls ou points insuffisants)")
            self.log("info", "Répartition prévue : " + ", ".join(f"{boost.STATS[c]} +{n}" for c, n in todo.items()) + f" ({used} points sur {points})")
            for cid, n in todo.items():
                field, verified = boost.fields()[cid]
                first = 1 if not verified else n
                for amount in ((first, n - first) if not verified and n > 1 else (n,)):
                    if amount <= 0: continue
                    before = dict(self.base); self._krg_event.clear()
                    self._send(boost.KRC, f_varint(field, amount)); self.log("action", f"→ krc : {boost.STATS[cid]} +{amount} (champ {field})")
                    if not self._wait_krg(): raise RuntimeError("le serveur n'a pas confirmé la répartition (pas de krg)")
                    moved = {c: self.base.get(c, 0) - before.get(c, 0) for c in boost.STATS if self.base.get(c, 0) != before.get(c, 0)}
                    spent = (before.get(boost.POINTS) or 0) - (self.base.get(boost.POINTS) or 0)
                    if cid in moved and moved[cid] >= amount:
                        if not verified: boost.verify(cid, field); verified = True; self.log("event", f"Champ {field} = {boost.STATS[cid]} confirmé")
                    elif moved:
                        real = max(moved, key=lambda c: moved[c])
                        boost.verify(real, field)
                        raise RuntimeError(f"calibrage : le champ {field} augmente {boost.STATS[real]} (et non {boost.STATS[cid]}) : noté, relance la répartition")
                    else: raise RuntimeError(f"aucun effet sur {boost.STATS[cid]} (points dépensés : {spent})")
                    time.sleep(0.4)
            self.log("event", f"Points répartis ; restants : {self.base.get(boost.POINTS)}")
        except Exception:
            if auto: boost.save_settings(self.aid, auto=False); self.log("error", "Répartition automatique désactivée après un échec")
            raise
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
        blocked = {c for a, c in self.actors.items() if a != self.char_id} | set(knowledge.load("bad_cells.json", {}).get(str(self.map_id), []))
        path = gamemaps.find_path(m, self.my_cell, int(to), blocked)
        if not path:      # the remembered bad cells are only a guess: without them, any way is better than none
            path = gamemaps.find_path(m, self.my_cell, int(to), {c for a, c in self.actors.items() if a != self.char_id})
        return path, None if path else "aucun chemin"

    # ---- actions (one at a time, in a worker thread; progress goes to the journal) ----
    def trace(self, kind, name, body):
        if self._trace is not None: self._trace.append((round(time.time(), 3), kind, name, body.hex()))

    def _send(self, name, body=b""):
        if self.conn is None: raise RuntimeError("non connecté")
        self.trace("C", name, body)
        self.conn.send(request(name, body))
        self._note_sent(name, body)

    def _note_sent(self, name, body):
        """Remember the last move / use / border change / attack: the next map change is explained by one of them (see _learn_passage)."""
        now = time.time()
        try:
            if name == login.JOK:
                raw = next((v for f, v in parse(body) if f == 1 and isinstance(v, bytes)), b""); i = 0; last = None
                while i < len(raw): v, i = dec_varint(raw, i); last = v & 0xFFF
                if last is not None: self._last["jok"] = (now, self.map_id, last)
            elif name == login.ITZ:
                stand = (self._last.get("jok") or (0, None, self.my_cell))[2] if self._last.get("jok") and self._last["jok"][1] == self.map_id else self.my_cell
                self._last["itz"] = (now, self.map_id, _vi(body, 1), _vi(body, 3), stand)
            elif name in (login.JOC, login.HMS): self._last["joc" if name == login.JOC else "hms"] = now
        except (ValueError, IndexError): pass

    def _learn_passage(self, prev, new):
        """A map change nobody explained yet: remember it, whoever caused it (this also learns what the player does by hand in the UI).
        An element used within 6 s = a door; a walk that ended within 12 s = a trigger cell; border changes (joc) and fights are skipped."""
        if not prev or not new or prev == new: return
        now = time.time(); L = self._last
        if now - L.get("hms", 0) < 20 or now - L.get("joc", 0) < 12: return
        if self.in_fight or time.monotonic() - self.fight.last_message_at < 40: return      # a fight map (start or end of a fight) is not a passage
        itz = L.get("itz")
        if itz and itz[1] == prev and now - itz[0] < 6:
            self.arrival_inside = True
            if gamedoors.record(prev, itz[2], itz[3], None, new, itz[4]): self.log("event", f"Passage appris : porte {itz[3]} de {prev} → {new} (depuis la case {itz[4]})")
            return
        jok = L.get("jok")
        if jok and jok[1] == prev and now - jok[0] < 12:
            m = gamemaps.load(prev)
            if m and any(jok[2] in gamemaps.exit_cells(m, side) for side in ("top", "bottom", "left", "right")): return      # a border: the neighbour graph already has it
            self.arrival_inside = True
            if gamedoors.record_step(prev, jok[2], new): self.log("event", f"Passage appris : la case {jok[2]} de {prev} mène à {new}")

    def _cancel(self):
        """A stop was asked: the session is closing, or the autopilot that drives this action was stopped."""
        return self._stop.is_set() or (self.auto.on and self.auto._stop.is_set())

    def _walk(self, path, attempts=3):
        """Send jok for `path`, wait for the server's jnz and the walking time, then confirm with jny."""
        keys = gamemaps.movement_keys(path)
        self._moved.clear(); self._mapchanged.clear()
        started = time.time()
        for attempt in range(attempts):     # after a refused request (joc, itz...) the server swallows the first move: send it once more; then close any open interface (kiq)
            if attempt == 2:
                self._send("kiq"); self.log("action", "→ kiq : fermer l'interface ouverte (longue-vue, menu...) avant de réessayer"); time.sleep(1.2)
            self._send(login.JOK, f_bytes(1, b"".join(enc_varint(k) for k in keys)) + f_varint(2, self.map_id))
            self.log("action", f"→ jok : {len(path) - 1} cases, de {path[0]} à {path[-1]}" + (f" ({attempt + 1}e envoi)" if attempt else ""))
            if self._moved.wait(3): break
        else:
            received = [(t, body) for ts, t, body in self.recent if ts >= started]
            detail = ", messages reçus : " + ", ".join(sorted({str(t) for t, _body in received})) if received else ""
            refusals = [",".join(str(value) for _field, value in parse(body) if isinstance(value, int))
                        for t, body in received if t == "jpc"]
            if refusals: detail += ", jpc fields : " + ";".join(refusals)
            if attempts > 1 and len(path) > 2:      # a long path is refused as a whole when one of its cells is: walk it cell by cell to find the culprit
                self._walk_stepwise(path); return
            raise RuntimeError("le serveur n'a pas confirmé le déplacement (jnz)" + detail)
        end = time.time() + gamemaps.duration(path)
        while time.time() < end:
            if self._cancel(): raise RuntimeError("arrêt demandé")
            if self._mapchanged.is_set(): return   # the last cell was a trigger: the server already loaded the next map (no jny)
            time.sleep(0.05)
        self._send(login.JNY); self.log("action", "→ jny (déplacement terminé)")

    def _walk_stepwise(self, path):
        """Walk a refused path one cell at a time; the cell the server refuses is remembered (knowledge/bad_cells.json, used by plan())."""
        self.log("warn", f"trajet refusé : marche case par case ({len(path) - 1} cases) pour trouver la case bloquante")
        for b in path[1:]:
            if self._mapchanged.is_set() or self._cancel(): return
            try: self._walk([self.my_cell, b], attempts=1)
            except RuntimeError:
                if self._mapchanged.is_set(): return
                try: time.sleep(1.0); self._walk([self.my_cell, b], attempts=1); continue      # a second try: an actor passing by also makes a step fail
                except RuntimeError: pass
                with knowledge._lock:
                    d = knowledge.load("bad_cells.json", {}); lst = d.setdefault(str(self.map_id), [])
                    if b not in lst: lst.append(b); knowledge.save("bad_cells.json", d)
                raise RuntimeError(f"le serveur refuse la case {b} de la carte {self.map_id} (notée, trajet à replanifier) (jnz)")
        self.log("info", "trajet terminé case par case")

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
        unconfirmed = 0
        for n, (_pref, _len, cell, path) in enumerate(cands[:4]):
            if unconfirmed >= 2: raise autopilot.DesyncError("le serveur refuse tous les déplacements : position désynchronisée")
            if self.my_cell != cell: path, _ = self.plan(cell)
            if not path: continue
            try:
                if len(path) > 1: self._walk(path)
            except RuntimeError as err:
                if self.map_id != start_map and self._mapchanged.is_set():
                    self._remember_exit(start_map, side, cell); time.sleep(1.0); return
                if self.status != "connected": raise
                self.log("warn", f"trajet vers la case de sortie {cell} refusé : {err}")
                if "(jnz)" in str(err): unconfirmed += 1
                continue
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
        if self._mapchanged.wait(4):      # a real door answers within a second (jow)
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
        arr = self.arrival_cell if self.arrival_inside else None      # across a border the arrival cell says nothing about the doors
        def man(a, b): (x, y), (X, Y) = gamemaps.to_xy(a), gamemaps.to_xy(b); return abs(x - X) + abs(y - Y)
        order = sorted((e for e in els if e not in known and e in pos and (els[e].get("skill") in skills or els[e].get("type") in gamejobs.door_types())),   # every door has its own skill id: they share the element type
                       key=lambda e: (man(gamemaps.estimate_cell(pos[e]), arr) if arr is not None else 0, e))      # the door we came through is next to where we arrived
        for e in order:
            near = sorted((c for c in range(gamemaps.CELLS) if arr is not None and gamemaps.walkable(m, c) and man(c, arr) <= 3), key=lambda c: (man(c, arr), c))[:5]
            pred = [c for c in gamemaps.stand_candidates(m, pos[e], max_cells) if c not in near[:2]]
            for c in near[:2] + pred + near[2:]:      # two cells next to the arrival first (the door we came through), then the predicted use-cells
                if self._cancel(): return False
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

    def use_for_objective(self, words, until, max_cells=6, strict=False):
        """Quest objective that wants an interactive element used (a book to read...), no element id given: try the elements of the map
        whose model name matches one of `words` first, then the others, each from the cells where its use-cell is predicted, until the
        objective is done. strict: skip elements whose model name does not match. Door-like results (map change) are learned as usual."""
        m = self.mapdata(); mid = self.map_id
        if not m: return False
        pos = gamemaps.element_positions(mid)
        known = {d["element"] for d in gamedoors.from_map(mid) if d.get("element")}
        with self._lock: els = {k: dict(v) for k, v in self.elements.items()}
        def name(e): return (gamedata.get("interactives", els[e]["interactive"]) or "") if els[e].get("interactive") is not None else ""
        # a named element that matches no word of the objective is a resource / workshop / zaap: never touched (harvesting locks the character)
        order = sorted((e for e in els if e not in known and els[e].get("skill") and
                        ((bool(name(e)) and any(w in name(e).lower() for w in words)) if strict else (not name(e) or any(w in name(e).lower() for w in words)))),
                       key=lambda e: (not name(e), e))
        for e in order:
            if self._cancel(): return False
            cells = (gamemaps.stand_candidates(m, pos[e], max_cells, 260) if e in pos else []) + [None]      # the current cell is the last resort
            for c in cells:
                self.log("info", f"Objectif : utilisation de l'élément {e} « {name(e) or '?'} » depuis la case {c if c is not None else 'actuelle'}")
                try:
                    if self._do_use(e, c, until):
                        self._send("kiq"); time.sleep(0.6)      # an examined object may leave its interface open (the first move would be swallowed)
                        return True
                except RuntimeError as err: self.log("info", f"  sans effet : {err}")
                if self.map_id != mid: return False      # it was a door: the caller travels back
        return False

    def _note_arrival(self, cell):
        """First position on a new map. An interior (no border exit) is left next to where we arrive: remember that cell as a guess of the way back."""
        left = getattr(self, "_left_map", None); mid = self.map_id
        if not left or not mid or left == mid: return
        if self.in_fight or time.monotonic() - self.fight.last_message_at < 40: return      # fight maps are not interiors
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
            if self._cancel(): raise RuntimeError("arrêt demandé")
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

    def _do_attack(self, actor, max_group=None):
        """Walk next to a monster group, attack it (hms) and play the whole fight with fightbot."""
        with self._lock: cell = self.actors.get(actor); mons = list(self.groups.get(actor, []))
        m = self.mapdata()
        if cell is None or not m: raise RuntimeError("groupe introuvable sur la carte")
        limit = fightbot.MAX_GROUP if max_group is None else max_group
        if len(mons) > limit: raise RuntimeError(f"groupe trop gros ({len(mons)} monstres, max {limit})")
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

    def _do_resume_fight(self):
        """The character is already in a fight (reconnection): the placement is over, only play the turns."""
        self.fight.over.clear(); self._trace = []
        try: self.fight.play(resume=True)
        finally: self._dump_trace([])

    def attack(self, actor):
        return self.start_action(f"Attaquer le groupe {actor}", lambda: self._do_attack(actor))

    # ---- quests and dialogs ----
    def questview(self):
        with self._lock: quests, dialog, npcs, actors = dict(self.quests), dict(self.dialog) if self.dialog else None, dict(self.npcs), dict(self.actors)
        d = None
        if dialog:
            tpl = npcs.get(dialog.get("actor")); d = dict(dialog, npc=gamedata.quest_data("npcs", tpl) if tpl else None)
        return {"auto": {"on": self.auto.on, "goal": self.auto.goal, "quest_id": self.auto.quest_id}, "live": self.status == "connected", "quests": [gamequests.quest_view(q, st, objs) for q, (st, objs) in quests.items()], "dialog": d,
                "npcs": [{"actor": a, "cell": actors.get(a), "name": gamedata.quest_data("npcs", t) or f"PNJ {t}"} for a, t in npcs.items()]}

    def _do_talk(self, actor):
        """Walk next to the NPC, then send ilt (action 3 = Parler) and wait for the dialog to open."""
        with self._lock: cell = self.actors.get(actor); mid = self.map_id
        m = self.mapdata()
        if cell is None or not m: raise RuntimeError("PNJ introuvable sur la carte")
        x, y = gamemaps.to_xy(cell)
        adjacent = {gamemaps.from_xy(x + dx, y + dy) for dx, dy in gamemaps.DIRS} - {None}
        if self.my_cell not in adjacent:
            best = None; paths = []
            for c in adjacent:
                path, _ = self.plan(c)
                if path: paths.append(path)
                if path and (best is None or len(path) < len(best)): best = path
            if best is None:      # NPC on an island (counter, stage...): get as close as the walkable floor allows and ask from there
                near = sorted((max(abs(a - x), abs(b - y)), c) for c in range(gamemaps.CELLS) if gamemaps.walkable(m, c) for a, b in [gamemaps.to_xy(c)])
                for d, c in near:
                    if d > 12: break      # scenery NPCs (a portal behind the map's playable area) are talked to from the nearest reachable cell
                    path, _ = self.plan(c)
                    if path: best = path; self.log("info", f"PNJ isolé : je m'approche à {d} case(s) (case {c})"); break
            if best is None: raise RuntimeError("aucune case accessible à côté du PNJ")
            if len(best) > 1:
                err = None
                for path in sorted(paths, key=len)[:4] if paths else [best]:      # the server may refuse one approach (jpc): try the other cells around the NPC
                    try: self._walk(path); err = None; break
                    except RuntimeError as e:
                        err = e
                        if "(jnz)" not in str(e): break
                        self.log("warn", f"approche refusée vers la case {path[-1]} : autre case autour du PNJ")
                if err: raise err
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
            self.in_fight = False
            self.fight.reset()
            self._stop = threading.Event(); self.status, self.error, self.since = "connecting", None, time.time()
        self.log("info", "Connexion en cours…")
        self._thread = threading.Thread(target=self._run, daemon=True); self._thread.start()
        def watchdog(stop=self._stop):   # the game server can stay silent (e.g. the character is still held by a previous session)
            time.sleep(CONNECT_TIMEOUT)
            if self.status == "connecting" and not stop.is_set():
                self.log("error", f"Aucune donnée de personnage après {CONNECT_TIMEOUT} s : le serveur ne répond pas"); stop.set()
        threading.Thread(target=watchdog, daemon=True).start()
        return True

    def reconnect(self, timeout=90):
        """Drop the game connection and open a new one (the server then tells us the real position). Does NOT stop the autopilot.
        -> True once connected again."""
        self.log("warn", "Reconnexion pour resynchroniser la position")
        self._stop.set()
        end = time.time() + 20
        while self.status in ("connecting", "connected") and time.time() < end: time.sleep(0.3)
        time.sleep(3)      # let the game server release the character
        if not self.start(): return False
        end = time.time() + timeout
        while self.status == "connecting" and time.time() < end: time.sleep(0.3)
        if self.status == "connected": time.sleep(3); return True      # let the map and the actors load
        return False

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
                if t in gamejobs.MESSAGES:
                    if t != gamejobs.IPZ: return
                    job = (gamejobs.job_xp(body) or {}).get("job")
                    state["msgs"] = [m for m in state["msgs"] if not (m[0] == gamejobs.IPZ and (gamejobs.job_xp(m[1]) or {}).get("job") == job)]   # the latest per job wins
                if t == gamestate.KRG: state["msgs"] = [m for m in state["msgs"] if m[0] != gamestate.KRG]   # the latest wins
                state["msgs"].append((t, body)); state["dirty"] = True
                if t == gamestate.KSC:
                    _cid, name = gamestate.selection(body)
                    with self._lock: self.character = name
                    self.log("event", f"Personnage : {name}")
                elif t == gamestate.KRG and self.status != "connected":
                    self.set_status("connected"); self.claim_achievements_later(8.0)      # validate what is waiting once the world is loaded
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
