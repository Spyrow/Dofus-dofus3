"""Quest autopilot: picks supported quest objectives, travels there, uses required elements or talks to the NPC and answers its dialog.

Supported objective types: 0/8 (use an element on a known or text-derived subarea map), 1 "Aller voir #1" and 9 "Retourner voir #1".
NOT supported (the autopilot stops and says why): fights (type 6, 7, 13, 14, 16: the combat protocol is not implemented), crafting,
item hand-ins, map discovery, anything whose NPC map is unknown, and map changes that need an interactive element (doors, zaaps).
It also stops when a fight starts, when the character disconnects, after MAX_ATTEMPTS failed tries on the same objective, or on request.

Travel uses %APPDATA%/dofus_net/mapgraph.json (tools/index_maps.py): {map id: [top, bottom, left, right] neighbour ids}."""
import collections, json, os, re, threading, time, unicodedata
import gamedata, gamedoors, gamequests, guide, knowledge, questbook, store

GRAPH_PATH = os.path.join(os.path.dirname(store.PATH), "mapgraph.json")
SIDES = ("top", "bottom", "left", "right")
TALK_TYPES = {1, 9}
FREE_ATTEMPTS = 8                   # free-text objectives may hide a specific dialog branch: more tries (each walks other replies)
USE_TYPES = {0, 8}                  # "read the book", "use ...": an interactive element of the objective's map
FIGHT_TYPES = {6, 7, 13, 14, 16}
MAX_ATTEMPTS = 3
FIGHT_REPLY = re.compile(r"combat|affront|battr|d[ée]fi|attaqu|[ée]prouve|provoqu", re.I)   # replies that start a boss fight
FIGHT_ATTEMPTS = 8        # fights allowed per objective (an objective may need several)
MAX_LLM_STEPS = 12         # actions suggested by the guide per run
MAX_SEARCH_MAPS = 25      # maps visited while looking for monsters
REST_SECONDS = 8          # pause between two fights
MAX_TRAVEL_STEPS = 80
# replies that end or refuse a conversation; the autopilot prefers any other reply (the first one), and falls back to the first reply
REFUSE = re.compile(r"renonc|refus|partir|quitter|s'en aller|laisser|abandon|fuir|pas int[ée]ress|plus tard|non,|saluer", re.I)

_graph = None


BLOCKED_FILE = "blocked_edges.json"   # {map id: [sides]}: borders declared in the map data that have no exit cell (learned, shared)


def blocked_edges():
    return knowledge.load(BLOCKED_FILE, {})


def block_edge(map_id, side):
    with knowledge._lock:
        b = blocked_edges(); lst = b.setdefault(str(map_id), [])
        if side not in lst: lst.append(side); knowledge.save(BLOCKED_FILE, b)


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
    g = graph(); doors = gamedoors.load(); dead = blocked_edges(); start, goal = int(start), int(goal)
    if start == goal: return []
    prev = {start: None}; queue = collections.deque([start])
    while queue:
        cur = queue.popleft()
        edges = [(nxt, ("side", side)) for side, nxt in zip(SIDES, g.get(cur, ())) if nxt and side not in dead.get(str(cur), ())]
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
    g = graph(); doors = gamedoors.load(); dead = blocked_edges(); start = int(start)
    dist = {start: 0}; queue = collections.deque([start])
    while queue:
        cur = queue.popleft()
        nxts = [n for side, n in zip(SIDES, g.get(cur, ())) if n and side not in dead.get(str(cur), ())] + [int(d["to"]) for d in doors.get(cur, ()) if d.get("to")]
        for n in nxts:
            if n not in dist: dist[n] = dist[cur] + 1; queue.append(n)
    return dist


def pick_reply(replies, prefer=None):
    if prefer:
        for r in replies:
            if prefer.search(r["text"]): return r
    for r in replies:
        if not REFUSE.search(r["text"]): return r
    return replies[0]


class DesyncError(RuntimeError):
    """The server refuses every move from the cell we believe we are on: our position is out of sync (reconnecting resets it)."""


class Autopilot:
    def __init__(self, sess):
        self.s = sess; self.explore = 0; self.discover_tries = collections.Counter(); self.on = False; self.goal = None; self.quest_id = None; self.kinds = {"talk", "fight"}; self.llm = False; self.llm_steps = 0; self._last_sync = 0.0; self.want_fight = False; self.fight_played = False; self._stuck = []; self._hinted = set()
        self._stop = threading.Event(); self._thread = None; self.attempts = collections.Counter(); self._location_maps = {}

    # ---- control ----
    def start(self, kinds=None, llm=False, quest_id=None):
        """kinds: subset of {"talk", "fight"} to handle (default both). llm: ask the Groq guide for dialog replies the book does not know,
        and for the next action when no objective can be handled. quest_id: restrict this run to one active quest."""
        if self.on: return False
        if self.s.status != "connected": return False
        if quest_id is not None:
            try: quest_id = int(quest_id)
            except (TypeError, ValueError): return False
            with self.s._lock:
                if quest_id not in self.s.quests: return False
        self.quest_id = quest_id
        self.kinds = set(kinds or ("talk", "fight")); self.llm = bool(llm) and guide.available(); self.llm_steps = 0; self._hinted = set()
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
                if self.s.in_fight:   # a fight already running (reconnection, manual start): play its turns instead of stopping
                    self.say("info", "un combat est en cours : je le reprends")
                    try: self._act("Reprise du combat", self.s._do_resume_fight)
                    except RuntimeError as e: self.say("warn", f"combat impossible : {e}"); break
                    time.sleep(REST_SECONDS); continue
                if self.s.dialog: self._dialog(); continue
                obj, why = self._pick()
                if obj is None and time.time() - self._last_sync > 20:   # the server does not push the next step by itself: ask for the fresh state, once in a while
                    self._last_sync = time.time()
                    try: self.s.sync_quests()
                    except Exception: pass
                    time.sleep(0.5); obj, why = self._pick()
                if obj is None:
                    if self.llm and self._unblock(): continue
                    if self.quest_id is None and self.llm and self.llm_steps < MAX_LLM_STEPS and self._ask_guide(why): continue
                    self.say("info", why); break
                o = gamedata.quest_data('objectives', obj[1]) or {}
                if not (self._fight_objective(*obj) if o.get('type') in FIGHT_TYPES else self._objective(*obj)):
                    self.say("info", f"objectif « {gamequests.objective_text(obj[1])} » abandonné" + ("" if self._alive() else " (arrêt demandé ou déconnexion)"))
                    if self.llm and self._alive():      # the bot gave up on it: maybe Groq knows where it really is
                        self._stuck = [obj]
                        if self._unblock(): continue
                    break
                time.sleep(0.5)
        except Exception as e:
            self.say("error", f"{type(e).__name__}: {e}")
        finally:
            self.on = False; self.goal = None; self.say("info", "arrêté")

    def _world(self, map_id):
        mi = gamedata.map_info(map_id) if map_id else None
        return mi["world"] if mi else None

    def _pick(self):
        """-> ((quest id, objective id), None) or (None, reason). Only objectives of the zone we are in (the starting zone, Astrub...); among
        them the closest one first (steps from the current map), so the walking is as short as possible."""
        unsupported = None; cands = []; self._stuck = []
        def block(qid, oid, msg):
            nonlocal unsupported
            self._stuck.append((qid, oid)); unsupported = unsupported or msg
        with self.s._lock: quests = {q: (st, dict(objs)) for q, (st, objs) in self.s.quests.items()}
        if self.quest_id is not None and self.quest_id not in quests:
            return None, "la quête sélectionnée est terminée ou n'est plus active"
        here = self._world(self.s.map_id); reach = distances(self.s.map_id) if self.s.map_id else {}
        for qid, (_step, objs) in quests.items():
            if self.quest_id is not None and qid != self.quest_id: continue
            for oid, active in objs.items():
                if not active: continue
                o = gamedata.quest_data("objectives", oid) or {}
                kind = "talk" if o.get("type") in TALK_TYPES | USE_TYPES else "fight" if o.get("type") in FIGHT_TYPES else None
                if kind and kind not in self.kinds: continue
                text = gamequests.objective_text(oid)
                if o.get("map") and here and self._world(int(o["map"])) not in (None, here): continue      # another zone: not our business now
                if o.get("type") in TALK_TYPES | USE_TYPES:
                    if self.attempts[oid] >= (FREE_ATTEMPTS if o.get("type") in USE_TYPES else MAX_ATTEMPTS):
                        block(qid, oid, f"objectif « {text} » : échec après {self.attempts[oid]} essais")
                    elif o.get("map"):
                        tgt = int(o["map"])
                        if self.s.map_id != tgt and route(self.s.map_id, tgt) is None and not self._can_discover(tgt):
                            block(qid, oid, f"objectif « {text} » : aucune route connue (porte non apprise ?)")
                        else: cands.append((0 if self.s.map_id == tgt else reach.get(tgt, 30), qid, oid))
                    elif o.get("type") == 0:
                        maps = self._objective_location_maps(text, reach)
                        if maps: cands.append((reach[maps[0]], qid, oid))
                        else: block(qid, oid, f"objectif « {text} » : sous-zone ou carte introuvable")
                    else: block(qid, oid, f"objectif « {text} » : carte inconnue")
                elif o.get("type") in FIGHT_TYPES and self.attempts[oid] < FIGHT_ATTEMPTS and (
                        int(o["p"][0]) in {m for ms in self.s.groups.values() for m in ms} or gamedata.monster_maps(int(o["p"][0])) or o.get("map")):
                    tgt = int(o["map"]) if o.get("map") else 0
                    here_group = int(o["p"][0]) in {m for ms in self.s.groups.values() for m in ms}
                    cands.append((0 if here_group else reach.get(tgt, 12) if tgt else 12, qid, oid))
                elif o.get("type") in FIGHT_TYPES: block(qid, oid, f"objectif « {text} » : aucun groupe de ce monstre sur la carte actuelle")
                else: block(qid, oid, f"objectif « {text} » : type non géré")
        if cands:
            _d, qid, oid = min(cands); return (qid, oid), None
        return None, ("aucun objectif géré — " + unsupported) if unsupported else "aucune quête en cours"

    def _objective_location_maps(self, text, reach=None, broad=False):
        """Resolve a mapless free-text objective to reachable maps in its subarea or the current world."""
        world = self._world(self.s.map_id)
        if not world: return []
        normalize = lambda value: "".join(c for c in unicodedata.normalize("NFKD", str(value)).casefold() if not unicodedata.combining(c))
        key = (world, normalize(text), broad)
        if key not in self._location_maps:
            matches = []
            for mid, raw in gamedata.data().get("maps", {}).items():
                if not isinstance(raw, list) or len(raw) < 4: continue
                info = gamedata.map_info(int(mid))
                if not info or info["world"] != world: continue
                if not broad:
                    if not info["subarea"]: continue
                    name = normalize(info["subarea"])
                    if not re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", key[1]): continue
                matches.append(int(mid))
            self._location_maps[key] = matches
        reach = distances(self.s.map_id) if reach is None else reach
        return sorted((mid for mid in self._location_maps[key] if mid in reach), key=lambda mid: (reach[mid], mid))

    # ---- LLM guide ----
    def _ask_guide(self, reason, instruction=None):
        """Ask Groq for the next actions and run the validated ones. -> True if something was executed."""
        state = self.s.guide_state()
        actions, why = guide.plan(state, instruction)
        self.say("info", f"guide Groq : {why or 'aucune suggestion'}" + (f" — {len(actions)} action(s)" if actions else ""))
        return self._run_actions(actions)

    def _hint_maps(self, hint):
        """Maps (reachable from here, closest first) that a quest hint {x, y, subarea} points to; [] if it matches nothing known."""
        world = self._world(self.s.map_id)
        if not world: return []
        norm = lambda v: "".join(c for c in unicodedata.normalize("NFKD", str(v or "")).casefold() if not unicodedata.combining(c)).strip()
        sub = norm(hint.get("subarea")); x, y = hint.get("x"), hint.get("y"); reach = distances(self.s.map_id); found = []
        for mid in reach:
            info = gamedata.map_info(mid)
            if not info or info["world"] != world: continue
            same_sub = bool(sub) and norm(info["subarea"]) == sub
            if x is not None and y is not None:
                if info["x"] == x and info["y"] == y: found.append((0 if same_sub or not sub else 1, reach[mid], mid))
            elif same_sub: found.append((0, reach[mid], mid))
        return [m for _a, _b, m in sorted(found)]

    def _unblock(self):
        """Objectives the bot cannot handle (no map, no route, unknown place...): ask Groq what it knows about the quest, go where it points
        and let the guide act there. Once per objective and run; hints are kept in the quest book. -> True if something was executed."""
        here = self.s.map_id; did = False
        for qid, oid in list(dict.fromkeys(self._stuck)):
            if oid in self._hinted or not self._alive(): continue
            self._hinted.add(oid)
            quest = (gamedata.quest_data("quests", qid) or {}).get("name"); text = gamequests.objective_text(oid)
            hint = questbook.get_hint(oid)
            if hint is None:
                info = gamedata.map_info(here) if here else None
                qd = gamedata.quest_data("quests", qid) or {}
                with self.s._lock: step_id = (self.s.quests.get(qid) or (None,))[0]
                sd = gamedata.quest_data("steps", step_id) or {} if step_id is not None else {}
                hint, err = guide.quest_hint(quest, text, f"{info['area']} / {info['subarea']} [{info['x']}, {info['y']}]" if info else None,
                                             qd.get("level"), sd.get("name"), sd.get("desc"))
                if err: self.say("warn", f"indice Groq indisponible : {err}"); continue
                if hint: questbook.record_hint(oid, qid, hint)
            if not hint: self.say("info", f"Groq ne connaît pas d'indice pour « {text} »"); continue
            self.say("info", f"indice Groq pour « {text} » : {hint.get('why') or ''} → {hint.get('subarea') or '?'} [{hint.get('x')}, {hint.get('y')}]" + (f", PNJ {hint['npc']}" if hint.get("npc") else ""))
            maps = self._hint_maps(hint)
            if not maps: self.say("warn", "l'indice ne correspond à aucune carte accessible"); continue
            if self.s.map_id != maps[0]:
                self.goal = f"{text} (indice Groq, carte {maps[0]})"
                if not self._travel(maps[0]): continue
            did = True
            order = f"Quête « {quest} », objectif : « {text} ». Indice : {hint.get('why') or ''}" + (f" Parle au PNJ « {hint['npc']} »." if hint.get("npc") else "")
            self._ask_guide("indice", order)
            time.sleep(1.0)
            try: self.s.sync_quests()
            except Exception: pass
        return did

    def _run_actions(self, actions):
        did = False
        for a in actions:
            if not self._alive() or self.llm_steps >= MAX_LLM_STEPS: break
            if a["action"] == "stop": break
            self.llm_steps += 1; did = True; self.say("action", f"guide → {a}")
            try:
                if a["action"] == "talk":
                    self._act(f"Parler au PNJ {a['npc']}", lambda: self.s._do_talk(a["npc"])); self._dialog()
                elif a["action"] == "change_map": self._act(f"Changer de carte ({a['side']})", lambda: self.s._do_change_map(a["side"]))
                elif a["action"] == "use_element": self._act(f"Utiliser l'élément {a['element']}", lambda: self.s._do_use(a["element"]))
                elif a["action"] == "go_map": self._travel(a["map_id"])
                elif a["action"] == "attack": self._act(f"Attaquer le groupe {a['group']}", lambda: self.s._do_attack(a["group"]))
            except RuntimeError as e:
                self.say("warn", f"action du guide impossible : {e}"); break
        return did

    def run_instruction(self, text):
        """A free instruction from the player ('va voir le capitaine') translated into bot actions by the guide, then executed."""
        if self.on or self.s.status != "connected": return "autopilote actif ou personnage non connecté"
        if not guide.available(): return "clé Groq non configurée"
        self.quest_id = None; self.on = True; self.llm_steps = 0; self._stop = threading.Event()
        def run():
            try:
                self.say("info", f"instruction : « {text} »")
                if not self._ask_guide("instruction", text): self.say("warn", "le guide n'a proposé aucune action utilisable")
            except Exception as e: self.say("error", f"{type(e).__name__}: {e}")
            finally: self.on = False; self.goal = None
        threading.Thread(target=run, daemon=True).start()
        return None

    # ---- fight objectives: attack groups containing the wanted monsters, searching nearby maps when none is here ----
    def _objective_maps(self):
        """Maps given by the active fight objectives themselves (a boss lives on one precise map)."""
        out = []
        with self.s._lock: quests = {q: dict(objs) for q, (_st, objs) in self.s.quests.items()}
        for qid, objs in quests.items():
            if self.quest_id is not None and qid != self.quest_id: continue
            for oid, active in objs.items():
                o = gamedata.quest_data("objectives", oid) or {}
                if active and o.get("type") in FIGHT_TYPES and o.get("map") and self.attempts[oid] < FIGHT_ATTEMPTS: out.append(int(o["map"]))
        return out

    def _wanted_monsters(self):
        """Monster ids of every active fight objective not yet exhausted."""
        out = set()
        with self.s._lock: quests = {q: dict(objs) for q, (_st, objs) in self.s.quests.items()}
        for qid, objs in quests.items():
            if self.quest_id is not None and qid != self.quest_id: continue
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
            key = (len(ms), -score, cell_dist(me, actors[a]) if me is not None and a in actors else 99)   # smallest group first: fewer enemies, fewer defeats
            if best is None or key < best[0]: best = (key, a)
        return best[1] if best else None

    def _boss_fight(self, oid):
        """Boss / quest fight started from a dialog: the monster is also an NPC standing on the objective's map. -> True if a fight was played."""
        o = gamedata.quest_data("objectives", oid) or {}
        mon = int(o["p"][0]); name = gamedata.quest_data("monsters", mon); npc_ids = gamedata.npc_ids_named(name) if name else []
        if not (npc_ids and o.get("map")): return False
        self.say("info", f"« {name} » est aussi un PNJ : combat par dialogue sur la carte {o['map']}")
        if not self._travel(int(o["map"])): return False
        actor = None
        for _ in range(8):
            with self.s._lock: actor = next((a for a, t in self.s.npcs.items() if t in npc_ids), None)
            if actor is not None or not self._alive(): break
            time.sleep(0.5)
        if actor is None: self.say("warn", f"{name} introuvable sur cette carte"); return False
        try: self._act(f"Parler au PNJ {actor}", lambda: self.s._do_talk(actor))
        except RuntimeError as e: self.say("warn", f"dialogue impossible : {e}"); return False
        self.want_fight = True; t_talk = time.time()
        try: self._dialog()
        finally: self.want_fight = False
        if not self._alive(): return False
        if self.fight_played: return True
        self._boss_played = False
        def fight(): self._boss_played = self.s._do_scripted_fight(t_talk)
        try: self._act("Combat de quête", fight)
        except RuntimeError as e: self.say("warn", f"combat impossible : {e}"); return False
        return self._boss_played

    def _fight_objective(self, qid, oid):
        """Fight the best group on this map; if there is none, visit the nearest map where a wanted monster lives (up to MAX_SEARCH_MAPS)."""
        self.attempts[oid] += 1
        if self._boss_fight(oid): return True
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
            dist = distances(cur); cands = ({m for w in wanted for m in gamedata.monster_maps(w)} | set(self._objective_maps())) - visited
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
    def _use_objective(self, qid, oid, target):
        text = gamequests.objective_text(oid); self.goal = f"{text} (carte {target})"
        self.attempts[oid] += 1
        self.say("info", f"objectif : {self.goal} — essai {self.attempts[oid]}/{FREE_ATTEMPTS}")
        if self.s.map_id != target and route(self.s.map_id, target) is None and not self._discover(target): return False
        if not self._travel(target): return False
        time.sleep(2.0)
        if re.match(r"\s*(parler|discuter|retourner|aller voir)", text.lower()):       # free-text "talk to ..." objective: the NPC is named in the text
            with self.s._lock: npcs = dict(self.s.npcs)
            low = text.lower()
            named = [a for a, t in npcs.items() if (gamedata.quest_data("npcs", t) or "~").lower() in low]
            order = named or sorted(npcs)         # no name in the text (a nickname): talk to the NPCs of the map one after the other
            self.explore = max(0, self.attempts[oid] - 1)
            for actor in order[:6]:
                if not self._alive(): return False
                try: self._act(f"Parler au PNJ {actor}", lambda: self.s._do_talk(actor))
                except RuntimeError as e: self.say("warn", f"dialogue impossible : {e}"); continue
                self._dialog(); time.sleep(1.5)
                try: self.s.sync_quests()
                except Exception: pass
                t0 = time.time()
                while self._alive() and time.time() - t0 < 4:
                    if not self.s.quests.get(qid, (None, {}))[1].get(oid, False): self.explore = 0; self.say("info", "objectif accompli"); return True
                    time.sleep(0.3)
            self.explore = 0
            self.say("warn", "objectif non validé après avoir parlé aux PNJ de la carte"); return self.attempts[oid] < FREE_ATTEMPTS
        words = [w for w in re.findall(r"[a-zéèêàç]{4,}", text.lower()) if w not in ("lire", "utiliser", "contant", "légende", "dans", "avec")]
        done = lambda: not self.s.quests.get(qid, (None, {}))[1].get(oid, False)
        try: ok = self.s.use_recorded(oid, done) or self.s.use_for_objective(words, done)
        except RuntimeError as e: self.say("warn", f"utilisation impossible : {e}"); return self.attempts[oid] < FREE_ATTEMPTS
        if not ok:
            try: self.s.sync_quests()
            except Exception: pass
            ok = done()
        if ok: self.say("info", "objectif accompli"); return True
        self.say("warn", "objectif non validé après l'essai des éléments de la carte"); return self.attempts[oid] < FREE_ATTEMPTS

    def _use_location_objective(self, qid, oid):
        text = gamequests.objective_text(oid); maps = self._objective_location_maps(text)
        if not maps:
            self.say("warn", f"objectif « {text} » : aucune carte accessible dans la sous-zone indiquée"); return False
        self.attempts[oid] += 1; self.goal = text
        self.say("info", f"objectif : {self.goal} — recherche dans {len(maps)} carte(s) accessible(s)")
        words = [w for w in re.findall(r"[a-zéèêàç]{4,}", text.lower()) if w not in ("lire", "utiliser", "contant", "légende", "dans", "avec")]
        if "vestige" in words: words = ["vestige"]
        done = lambda: not self.s.quests.get(qid, (None, {}))[1].get(oid, False)
        searched = set(); travel_failed = False
        def scan(candidates):
            nonlocal travel_failed
            for mid in candidates:
                if not self._alive(): return False
                if done(): self.say("info", "objectif accompli"); return True
                searched.add(mid)
                if self.s.map_id != mid and not self._travel(mid):
                    travel_failed = True; return False
                with self.s._lock: elements = dict(self.s.elements)
                names = [gamedata.get("interactives", e.get("interactive")) or "" for e in elements.values()]
                if not any(any(w in name.casefold() for w in words) for name in names): continue
                self.say("info", f"élément correspondant trouvé sur la carte {mid}")
                t0 = time.time()
                try: ok = self.s.use_for_objective(words, done, strict=True)
                except RuntimeError as e:
                    self.say("warn", f"utilisation impossible sur la carte {mid} : {e}"); continue
                if ok or done(): self.say("info", "objectif accompli"); return True
                if any(ts >= t0 and t == "hgq" for ts, t, _body in list(self.s.recent)):
                    self.s._send("kiq"); self.say("info", "menu du zaap fermé après l'examen")
            return done()
        if scan(maps): return True
        if travel_failed or not self._alive(): return False
        broader = [mid for mid in self._objective_location_maps(text, broad=True) if mid not in searched]
        if broader:
            self.say("info", f"aucun élément correspondant dans la sous-zone : recherche étendue à {len(broader)} autre(s) carte(s) de la zone")
            if scan(broader): return True
        self.say("warn", f"aucun élément correspondant trouvé dans les cartes accessibles pour « {text} »")
        return False

    def _objective(self, qid, oid):
        o = gamedata.quest_data("objectives", oid); target = int(o.get("map") or 0)
        if o.get("type") in USE_TYPES:
            return self._use_objective(qid, oid, target) if target else self._use_location_objective(qid, oid)
        npc_id = int(o["p"][0])
        self.goal = f"{gamequests.objective_text(oid)} (carte {target})"
        self.attempts[oid] += 1
        self.say("info", f"objectif : {self.goal} — essai {self.attempts[oid]}/{MAX_ATTEMPTS}")
        if self.s.map_id != target and route(self.s.map_id, target) is None and not self._discover(target): return False
        if not self._travel(target): return False
        actor = self._find_npc(npc_id)
        if actor is None:
            self.say("warn", f"PNJ {gamedata.quest_data('npcs', npc_id) or npc_id} introuvable sur la carte"); return self.attempts[oid] < MAX_ATTEMPTS
        try: self._act(f"Parler au PNJ {actor}", lambda: self.s._do_talk(actor))
        except RuntimeError as e: self.say("warn", f"dialogue impossible : {e}"); return self.attempts[oid] < MAX_ATTEMPTS
        self._dialog()
        time.sleep(1.5)
        try: self.s.sync_quests()   # a finished quest sends no message of its own: ask for each quest, drop the silent ones
        except Exception: pass
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

    def _discover_maps(self, goal):
        """Reachable maps close to the goal's coordinates (same square first): the building's door is usually on one of them."""
        gi = gamedata.map_info(goal)
        if not gi: return []
        reach = distances(self.s.map_id); out = []
        for mid, steps in reach.items():
            mi = gamedata.map_info(mid)
            if not mi or mid == goal or mi["world"] != gi["world"]: continue
            d = max(abs(mi["x"] - gi["x"]), abs(mi["y"] - gi["y"]))
            if d <= 1: out.append((d, steps, mid))
        return [mid for _d, _s, mid in sorted(out)][:6]

    def _can_discover(self, goal):
        return self.discover_tries[goal] < 2 and bool(self._discover_maps(goal))

    def _discover(self, goal):
        """No known route to `goal`: visit the maps around it and probe their unknown doors until a route appears."""
        self.discover_tries[goal] += 1
        self.say("info", f"aucune route vers {goal} : découverte automatique des portes sur les cartes voisines")
        for mid in self._discover_maps(goal):
            for _ in range(12):
                if not self._alive(): return False
                if self.s.map_id != mid:
                    if route(self.s.map_id, mid) is None: break
                    if not self._travel(mid): break
                if not self.s.probe_doors():
                    self.say("info", f"carte {mid} : plus de porte à essayer"); break
                if route(self.s.map_id, goal) is not None or self.s.map_id == goal: self.say("event", "route trouvée vers l'objectif"); return True
                self.say("info", "porte apprise mais ce n'est pas la bonne : retour et poursuite de la découverte")
        return route(self.s.map_id, goal) is not None

    def _travel(self, goal):
        """Walk/use doors until map `goal` is reached. A step must change the map: if it does not, give up instead of looping."""
        prev = None; blocked_retries = 0; resyncs = 0
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
                elif arg.get("kind") == "step": self._act(f"Marcher sur la case {arg['cell']}", lambda: self.s._do_step(arg["cell"], bool(arg.get("guess"))))
                else: self._act(f"Utiliser l'élément {arg['element']}", lambda: self.s._do_use(arg["element"]))
            except DesyncError as e:
                resyncs += 1
                if resyncs > 2: self.say("error", f"voyage interrompu : {e}"); return False
                self.say("warn", f"{e} — reconnexion ({resyncs}/2)")
                if not self.s.reconnect(): self.say("error", "reconnexion impossible"); return False
                prev = None; continue
            except RuntimeError as e:
                if kind == "side" and ("case de sortie" in str(e) or "carte voisine" in str(e)) and blocked_retries < 3:
                    block_edge(cur, arg); blocked_retries += 1; prev = None   # this border cannot be crossed on foot: remember it and plan another way
                    self.say("warn", f"sortie {arg} impossible depuis la carte {cur} (notée) : nouvel itinéraire"); continue
                self.say("error", f"voyage interrompu : {e}"); return False
        self.say("error", "voyage trop long, abandon"); return False

    def _dialog(self):
        t_dialog = time.time()
        try: self._dialog_steps()
        finally: self.fight_played = self._dialog_fight(t_dialog)

    def _dialog_fight(self, since):
        """A reply may start a fight (the thief -> the evil fire...): play it here instead of letting the main loop stop on it."""
        end = time.time() + 3
        while not self.s.in_fight and time.time() < end and self._alive(): time.sleep(0.2)   # jxi can lag a little behind the end of the dialog
        if not self.s.in_fight or not self._alive(): return False
        self.say("info", "un combat a été déclenché par le dialogue : je le joue")
        played = []
        try: self._act("Combat de quête", lambda: played.append(self.s._do_scripted_fight(since)))
        except RuntimeError as e: self.say("warn", f"combat impossible : {e}"); return False
        time.sleep(REST_SECONDS)
        return bool(played and played[0])

    def _dialog_steps(self):
        for depth in range(30):
            d = self.s.dialog
            if not d or not self._alive(): return
            replies = d.get("replies") or []
            if not replies: time.sleep(1.5); self.s.close_dialog(); return
            d = self.s._dlg or {}
            r = questbook.suggest_reply(d.get("npc"), d.get("map"), d.get("path", []), replies, self.s._active_objectives(), REFUSE)
            src = "livre de quêtes" if r else "heuristique"
            if r is None and self.llm:
                quest = "; ".join(f"{o['quête']} : {o['objectif']}" for o in self.s.guide_state()["objectifs_actifs"][:4])
                npc = gamedata.quest_data("npcs", d.get("npc")) if d.get("npc") else None
                g, why = guide.choose_reply(quest, npc, (self.s.dialog or {}).get("text"), replies)
                if g: r, src = g, f"Groq : {why}"
            if r is None and self.explore:       # retry of a free-text talk objective: walk other branches of the dialog (reply index shifts with the attempt and the depth)
                r = replies[(self.explore + depth) % len(replies)]; src = f"exploration {self.explore}"
            r = r or pick_reply(replies, FIGHT_REPLY if self.want_fight else None)
            self.say("info", f"réponse ({src}) : « {r['text']} »")
            try: self._act(f"Répondre ({r['id']})", lambda: self.s._do_reply(r["id"]))
            except RuntimeError as e: self.say("warn", f"réponse impossible : {e}"); return
            time.sleep(0.4)
