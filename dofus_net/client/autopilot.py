"""Quest autopilot: picks the next supported quest objective, travels there (map graph), talks to the NPC and answers its dialog.

Supported objective types: 1 "Aller voir #1" and 9 "Retourner voir #1" (talk to an NPC on a known map).
NOT supported (the autopilot stops and says why): fights (type 6, 7, 13, 14, 16: the combat protocol is not implemented), crafting,
item hand-ins, map discovery, anything whose NPC map is unknown, and map changes that need an interactive element (doors, zaaps).
It also stops when a fight starts, when the character disconnects, after MAX_ATTEMPTS failed tries on the same objective, or on request.

Travel uses %APPDATA%/dofus_net/mapgraph.json (tools/index_maps.py): {map id: [top, bottom, left, right] neighbour ids}."""
import collections, json, os, re, threading, time
import gamedata, gamedoors, gamequests, store

GRAPH_PATH = os.path.join(os.path.dirname(store.PATH), "mapgraph.json")
SIDES = ("top", "bottom", "left", "right")
TALK_TYPES = {1, 9}
FIGHT_TYPES = {6, 7, 13, 14, 16}
MAX_ATTEMPTS = 3
FIGHT_ATTEMPTS = 8        # fights allowed per objective (an objective may need several)
MAX_SEARCH_MAPS = 25      # maps visited while looking for monsters
REST_SECONDS = 8          # pause between two fights
MAX_TRAVEL_STEPS = 80
# replies that end or refuse a conversation; the autopilot prefers any other reply (the first one), and falls back to the first reply
REFUSE = re.compile(r"renonc|refus|partir|quitter|s'en aller|laisser|abandon|fuir|pas int[ée]ress|plus tard|non,|saluer", re.I)

_graph = None


def graph():
    global _graph
    if _graph is None:
        try:
            with open(GRAPH_PATH, encoding="utf8") as f: _graph = {int(k): v for k, v in json.load(f).items()}
        except (OSError, ValueError): _graph = {}
    return _graph


def route(start, goal):
    """Shortest list of steps from map `start` to map `goal`, or None. A step is ('side', 'right'|'left'|'top'|'bottom') or
    ('door', {skill, element, cell, to}) from the learned passages (gamedoors)."""
    g = graph(); doors = gamedoors.load(); start, goal = int(start), int(goal)
    if start == goal: return []
    prev = {start: None}; queue = collections.deque([start])
    while queue:
        cur = queue.popleft()
        edges = [(nxt, ("side", side)) for side, nxt in zip(SIDES, g.get(cur, ())) if nxt]
        edges += [(int(d["to"]), ("door", d)) for d in doors.get(cur, ()) if d.get("to")]
        for nxt, step in edges:
            if nxt in prev: continue
            prev[nxt] = (cur, step)
            if nxt == goal:
                out = []
                while prev[nxt]: out.append(prev[nxt][1]); nxt = prev[nxt][0]
                return out[::-1]
            queue.append(nxt)
    return None


def distances(start):
    """BFS over maps (sides + learned doors): {map id: number of steps} from `start`."""
    g = graph(); doors = gamedoors.load(); start = int(start)
    dist = {start: 0}; queue = collections.deque([start])
    while queue:
        cur = queue.popleft()
        nxts = [n for n in g.get(cur, ()) if n] + [int(d["to"]) for d in doors.get(cur, ()) if d.get("to")]
        for n in nxts:
            if n not in dist: dist[n] = dist[cur] + 1; queue.append(n)
    return dist


def pick_reply(replies):
    for r in replies:
        if not REFUSE.search(r["text"]): return r
    return replies[0]


class Autopilot:
    def __init__(self, sess):
        self.s = sess; self.on = False; self.goal = None; self.kinds = {"talk", "fight"}
        self._stop = threading.Event(); self._thread = None; self.attempts = collections.Counter()

    # ---- control ----
    def start(self, kinds=None):
        """kinds: subset of {"talk", "fight"} to handle (default both)."""
        if self.on: return False
        self.kinds = set(kinds or ("talk", "fight"))
        if self.s.status != "connected": return False
        self.on = True; self.goal = None; self._stop = threading.Event(); self.attempts.clear()
        self._thread = threading.Thread(target=self._run, daemon=True); self._thread.start()
        return True

    def stop(self):
        self._stop.set()

    def say(self, level, msg):
        self.s.log(level, "Autopilote : " + msg)

    def _alive(self):
        return not self._stop.is_set() and self.s.status == "connected"

    def _act(self, label, fn):
        """Run one atomic action under the session's action lock (shared with the UI buttons)."""
        if not self.s._act.acquire(timeout=30): raise RuntimeError("une autre action est en cours")
        try:
            with self.s._lock: self.s.busy = label
            self.s.log("action", label); fn()
        finally:
            with self.s._lock: self.s.busy = None
            self.s._act.release()

    # ---- main loop ----
    def _run(self):
        self.say("info", "démarré")
        try:
            while self._alive():
                if self.s.in_fight: self.say("warn", "un combat est en cours (hors autopilote), arrêt"); break
                if self.s.dialog: self._dialog(); continue
                obj, why = self._pick()
                if obj is None: self.say("info", why); break
                o = gamedata.quest_data('objectives', obj[1]) or {}
                if not (self._fight_objective(*obj) if o.get('type') in FIGHT_TYPES else self._objective(*obj)): break
                time.sleep(0.5)
        except Exception as e:
            self.say("error", f"{type(e).__name__}: {e}")
        finally:
            self.on = False; self.goal = None; self.say("info", "arrêté")

    def _pick(self):
        """-> ((quest id, objective id), None) or (None, reason)."""
        unsupported = None
        with self.s._lock: quests = {q: (st, dict(objs)) for q, (st, objs) in self.s.quests.items()}
        for qid, (_step, objs) in quests.items():
            for oid, active in objs.items():
                if not active: continue
                o = gamedata.quest_data("objectives", oid) or {}
                kind = "talk" if o.get("type") in TALK_TYPES else "fight" if o.get("type") in FIGHT_TYPES else None
                if kind and kind not in self.kinds: continue
                text = gamequests.objective_text(oid)
                if o.get("type") in TALK_TYPES and o.get("map"):
                    if self.attempts[oid] >= MAX_ATTEMPTS: unsupported = unsupported or f"objectif « {text} » : échec après {MAX_ATTEMPTS} essais"
                    elif self.s.map_id != int(o["map"]) and route(self.s.map_id, int(o["map"])) is None:
                        unsupported = unsupported or f"objectif « {text} » : aucune route connue (porte non apprise ?)"; continue
                    else: return (qid, oid), None
                elif o.get("type") in FIGHT_TYPES and self.attempts[oid] < FIGHT_ATTEMPTS and (
                        int(o["p"][0]) in {m for ms in self.s.groups.values() for m in ms} or gamedata.monster_maps(int(o["p"][0]))):
                    return (qid, oid), None
                elif o.get("type") in FIGHT_TYPES: unsupported = unsupported or f"objectif « {text} » : aucun groupe de ce monstre sur la carte actuelle"
                else: unsupported = unsupported or f"objectif « {text} » : type non géré"
        return None, ("aucun objectif géré — " + unsupported) if unsupported else "aucune quête en cours"

    # ---- fight objectives: attack groups containing the wanted monsters, searching nearby maps when none is here ----
    def _wanted_monsters(self):
        """Monster ids of every active fight objective not yet exhausted."""
        out = set()
        with self.s._lock: quests = {q: dict(objs) for q, (_st, objs) in self.s.quests.items()}
        for objs in quests.values():
            for oid, active in objs.items():
                o = gamedata.quest_data("objectives", oid) or {}
                if active and o.get("type") in FIGHT_TYPES and self.attempts[oid] < FIGHT_ATTEMPTS: out.add(int(o["p"][0]))
        return out

    def _best_group(self, wanted):
        """Group of the current map covering the most wanted monsters (then the smallest, then the nearest), or None."""
        from fightbot import MAX_GROUP, dist as cell_dist
        with self.s._lock: groups = {a: list(ms) for a, ms in self.s.groups.items()}; actors = dict(self.s.actors); me = self.s.my_cell
        best = None
        for a, ms in groups.items():
            score = len(set(ms) & wanted)
            if not score or len(ms) > MAX_GROUP: continue
            key = (-score, len(ms), cell_dist(me, actors[a]) if me is not None and a in actors else 99)
            if best is None or key < best[0]: best = (key, a)
        return best[1] if best else None

    def _fight_objective(self, qid, oid):
        """Fight the best group on this map; if there is none, visit the nearest map where a wanted monster lives (up to MAX_SEARCH_MAPS)."""
        self.attempts[oid] += 1
        visited = set(); travel_failures = 0
        for _ in range(MAX_SEARCH_MAPS + 1):
            if not self._alive(): return False
            wanted = self._wanted_monsters()
            if not wanted: self.say("info", "plus de monstre à vaincre"); return True
            names = ", ".join(sorted(gamedata.quest_data("monsters", m) or str(m) for m in wanted))
            self.goal = f"vaincre : {names}"
            group = self._best_group(wanted)
            if group is not None:
                with self.s._lock: ms = list(self.s.groups.get(group, []))
                self.say("info", f"combat contre {', '.join(gamedata.quest_data('monsters', m) or str(m) for m in ms)}")
                try: self._act(f"Attaquer le groupe {group}", lambda: self.s._do_attack(group))
                except RuntimeError as e: self.say("warn", f"combat impossible : {e}"); return self.attempts[oid] < FIGHT_ATTEMPTS
                time.sleep(REST_SECONDS)   # the character regains a little life before the next fight; also lets the quest updates arrive
                return True
            cur = self.s.map_id; visited.add(cur)
            dist = distances(cur); cands = {m for w in wanted for m in gamedata.monster_maps(w)} - visited
            reachable = sorted((dist[m], m) for m in cands if m in dist)
            if not reachable: self.say("warn", "aucune carte accessible où ces monstres apparaissent"); return False
            nxt = reachable[0][1]
            self.say("info", f"aucun groupe adapté ici : recherche sur la carte {nxt} ({reachable[0][0]} étape(s))")
            if not self._travel(nxt):
                visited.add(nxt); travel_failures += 1
                if travel_failures >= 2: self.say("error", "déplacements refusés à répétition : session à relancer, arrêt"); return False
            else: travel_failures = 0; time.sleep(2.0)   # let the map's actors arrive
        self.say("warn", "recherche de monstres abandonnée (trop de cartes visitées)"); return False

    # ---- one objective: travel, talk, answer ----
    def _objective(self, qid, oid):
        o = gamedata.quest_data("objectives", oid); npc_id, target = int(o["p"][0]), int(o["map"])
        self.goal = f"{gamequests.objective_text(oid)} (carte {target})"
        self.attempts[oid] += 1
        self.say("info", f"objectif : {self.goal} — essai {self.attempts[oid]}/{MAX_ATTEMPTS}")
        if not self._travel(target): return False
        actor = self._find_npc(npc_id)
        if actor is None:
            self.say("warn", f"PNJ {gamedata.quest_data('npcs', npc_id) or npc_id} introuvable sur la carte"); return self.attempts[oid] < MAX_ATTEMPTS
        try: self._act(f"Parler au PNJ {actor}", lambda: self.s._do_talk(actor))
        except RuntimeError as e: self.say("warn", f"dialogue impossible : {e}"); return self.attempts[oid] < MAX_ATTEMPTS
        self._dialog()
        t0 = time.time()   # the server confirms with ibg/icj shortly after the dialog ends
        while self._alive() and time.time() - t0 < 4:
            if not self.s.quests.get(qid, (None, {}))[1].get(oid, False): self.say("info", "objectif accompli"); return True
            time.sleep(0.3)
        self.say("warn", "objectif non validé après le dialogue"); return self.attempts[oid] < MAX_ATTEMPTS

    def _find_npc(self, npc_id):
        for _ in range(8):
            with self.s._lock: found = next((a for a, t in self.s.npcs.items() if t == npc_id), None)
            if found is not None or not self._alive(): return found
            time.sleep(0.5)
        return None

    def _travel(self, goal):
        """Walk/use doors until map `goal` is reached. A step must change the map: if it does not, give up instead of looping."""
        prev = None
        for _ in range(MAX_TRAVEL_STEPS):
            cur = self.s.map_id
            if not self._alive(): return False
            if cur == goal: return True
            if cur == prev: self.say("error", "le voyage n'avance plus (la carte n'a pas changé après l'étape), abandon"); return False
            r = route(cur, goal)
            if r is None: self.say("error", f"aucune route de la carte {cur} à la carte {goal} (porte non apprise, zaap ou carte inconnue ?)"); return False
            kind, arg = r[0]
            self.say("info", f"voyage vers {goal} : {len(r)} étape(s), prochaine : " + (arg if kind == "side" else f"case-déclencheur {arg['cell']}" if arg.get("kind") == "step" else f"porte (élément {arg['element']})"))
            prev = cur
            try:
                if kind == "side": self._act(f"Changer de carte ({arg})", lambda: self.s._do_change_map(arg))
                elif arg.get("kind") == "step": self._act(f"Marcher sur la case {arg['cell']}", lambda: self.s._do_step(arg["cell"]))
                else: self._act(f"Utiliser l'élément {arg['element']}", lambda: self.s._do_use(arg["element"]))
            except RuntimeError as e: self.say("error", f"voyage interrompu : {e}"); return False
        self.say("error", "voyage trop long, abandon"); return False

    def _dialog(self):
        for _ in range(30):
            d = self.s.dialog
            if not d or not self._alive(): return
            replies = d.get("replies") or []
            if not replies: time.sleep(1.5); return
            r = pick_reply(replies)
            self.say("info", f"réponse : « {r['text']} »")
            try: self._act(f"Répondre ({r['id']})", lambda: self.s._do_reply(r["id"]))
            except RuntimeError as e: self.say("warn", f"réponse impossible : {e}"); return
            time.sleep(0.4)
