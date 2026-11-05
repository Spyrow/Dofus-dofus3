"""Quest autopilot: picks supported quest objectives, travels there, uses required elements or talks to the NPC and answers its dialog.

Supported objective types: 0/8 (use an element on a known or text-derived subarea map), 1 "Aller voir #1" and 9 "Retourner voir #1".
NOT supported (the autopilot stops and says why): fights (type 6, 7, 13, 14, 16: the combat protocol is not implemented), crafting,
item hand-ins, map discovery, anything whose NPC map is unknown, and map changes that need an interactive element (doors, zaaps).
It also stops when a fight starts, when the character disconnects, after MAX_ATTEMPTS failed tries on the same objective, or on request.

Travel uses %APPDATA%/dofus_net/mapgraph.json (tools/index_maps.py): {map id: [top, bottom, left, right] neighbour ids}."""
import collections, json, os, re, threading, time, unicodedata
import challenges, dpln, notify, gamedata, gamedoors, gamejobs, gamemaps, gamequests, guide, guideorder, guidesteps, knowledge, questbook, store, tutorial

GRAPH_PATH = os.path.join(os.path.dirname(store.PATH), "mapgraph.json")
SIDES = ("top", "bottom", "left", "right")
TALK_TYPES = {1, 2, 3, 9}           # 3 = "Ramener à #1 : x#3 #2" (the NPC takes the item in its dialog); 2 = "Montrer à #1 : #3 #2" (p = [npc, item, quantity]): the NPC's dialog takes the item
FREE_ATTEMPTS = 8                   # free-text objectives may hide a specific dialog branch: more tries (each walks other replies)
USE_TYPES = {0, 8}                  # "read the book", "use ...": an interactive element of the objective's map
FIGHT_TYPES = {6, 7, 13, 14, 16}
DISCOVER_TYPE = 4                   # "Découvrir la carte : #1": done when the character walks into that map
CRAFT_TYPE = 17                     # "Fabriquer #2 #1 et fermer l'interface": p = [item, quantity, 0], map = the workshop's map
RESOURCE_MAPS = "resource_maps.json"   # {interactive model id: [map ids where it was seen]}
MAX_GATHER_MAPS = 30
MAX_HUNT_FIGHTS = 12                # fights allowed to collect one monster drop
UNVALIDATED_FILE = "unvalidated_objectives.json"   # {objective id: text}: use objectives naming an element that is not on their map, never validated
GATHER_FAILED_FILE = "gather_failed.json"   # {objective id: time}: crafts whose resource nobody found
START_FAILED_FILE = "start_failed.json"      # per character: quests whose NPC did not start them, with the level at the time
GATHER_RETRY_HOURS = 6
RECONNECT_WINDOW = 1200            # seconds over which the reconnections are counted
MAX_RECONNECTS = 5                  # reconnections after a dropped connection before the autopilot gives up
MAX_DOOR_PROBES = 10                # unknown doors tried in one search for a resource
MAX_BORDER_EXPLORES = 25            # maps whose unknown borders are tried in one run
MAX_SILENT_SPOTS = 3                # resource spots in a row that never answer before the gathering is given up
REGEN_SECONDS = 2.0                # natural life regeneration out of fight: about 1 HP per 2 s
MAX_NEW_QUESTS = 3                 # quests the autopilot starts by itself in one run
GEAR_SETS = [("Piou Bleu", 491, [8244, 8232, 8214, 8220, 8238, 8226])]     # (set name, monster id, [hat, cape, amulet, ring, belt, boots]) farmed once Incarnam is done
GEAR_SLOTS = {16: (6,), 17: (7,), 1: (0,), 9: (2, 4), 10: (3,), 11: (5,)}     # item type -> equipment positions to try
MAX_GEAR_FIGHTS = 80
MAX_GEAR_TRAVEL_FAILURES = 3      # failed trips to hunting maps after which the set farm is given up for this run
MAX_FIGHT_GROUP = 3                 # biggest group the fight AI takes without dying (fightbot.MAX_GROUP)
QUEST_MAX_GROUP = 8                 # quest fights and hunts take any group on the map (the defeats are tracked per objective); the gear farm stays picky
GEAR_MAX_GROUP = 2                  # biggest group attacked for the set: three monsters killed the level 8 character
MAX_OBJECTIVE_DEATHS = 2          # deaths on one objective before it is left alone
DANGER_FORGIVE_LEVELS = 3          # levels gained since the last defeat on an objective before it is tried again
MAX_DEFEATS = 3                     # defeats allowed in one autopilot run before it stops (a dungeon fight that keeps being lost)
MAX_ATTEMPTS = 3
FIGHT_REPLY = re.compile(r"combat|affront|battr|d[ée]fi|attaqu|[ée]prouve|provoqu", re.I)   # replies that start a boss fight
FIGHT_ATTEMPTS = 8        # fights allowed per objective (an objective may need several)
DUNGEON_WORDS = ("crypte", "tombeau", "donjon", "grange")
MAX_LLM_STEPS = 12         # actions suggested by the guide per run
MAX_SEARCH_MAPS = 25      # maps visited while looking for monsters
REST_SECONDS = 8          # pause between two fights
MAX_TRAVEL_STEPS = 80
REFRESH_AFTER = None      # connection renewal removed (see Autopilot._refresh_connection)
HELP_WAIT = 300      # seconds the autopilot waits for the user when it cannot find an exit
# replies that end or refuse a conversation; the autopilot prefers any other reply (the first one), and falls back to the first reply
REFUSE = re.compile(r"renonc|refus|(?:dois|dois bien|vais|faut) partir|partir (?:maintenant|plus tard|sans)|rester|quitter|s'en aller|laisser|abandon|fuir|pas int[ée]ress|plus tard|non,|saluer", re.I)

_graph = None


def _danger_entry(raw):
    """dangerous_objectives.json value -> {"n": defeats, "level": character level at the last one}; older files held the bare count."""
    return raw if isinstance(raw, dict) else {"n": int(raw or 0), "level": 0}


BLOCKED_FILE = "blocked_edges.json"   # {map id: [sides]}: borders declared in the map data that have no exit cell (learned, shared)


_SESSION_BLOCKED = {}      # {map id: [sides]} that failed this run although the map data has exit cells there (unreachable from where we stood): not shared, forgotten at restart

def blocked_edges():
    b = {k: list(v) for k, v in knowledge.load(BLOCKED_FILE, {}).items()}
    for k, sides in _SESSION_BLOCKED.items():
        lst = b.setdefault(k, [])
        for s in sides:
            if s not in lst: lst.append(s)
    return b


def block_edge(map_id, side):
    try:
        if gamemaps.exit_cells(gamemaps.load(int(map_id)), side):      # the map data has exit cells there: not a dead border for good, but they could not be reached this time: avoid it for this run
            lst = _SESSION_BLOCKED.setdefault(str(map_id), [])
            if side not in lst: lst.append(side)
            return
    except Exception: pass
    with knowledge._lock:
        b = knowledge.load(BLOCKED_FILE, {}); lst = b.setdefault(str(map_id), [])      # the shared file only: the session entries must not leak into it
        if side not in lst: lst.append(side); knowledge.save(BLOCKED_FILE, b)


def graph():
    global _graph
    if _graph is None:
        try:
            with open(GRAPH_PATH, encoding="utf8") as f: _graph = {int(k): v for k, v in json.load(f).items()}
        except (OSError, ValueError): _graph = {}
    return _graph


def border_edges(cur):
    """[(to, ('side', side))] from knowledge/border_targets.json: borders whose real neighbour is not the one in the map file (interiors)."""
    out = []
    for k, to in knowledge.load("border_targets.json", {}).items():
        m, side = k.split(":")
        if int(m) == int(cur): out.append((int(to), ("side", side)))
    return out


def route(start, goal, use_dead=True):
    """Shortest list of steps from map `start` to map `goal`, or None. A step is ('side', 'right'|'left'|'top'|'bottom') or
    ('door', {skill, element, cell, to}) from the learned passages (gamedoors). Borders noted as blocked are avoided, but they are only a
    guess (a failed walk): when nothing else leads there they are tried again."""
    r = _route(start, goal, blocked_edges() if use_dead else {})
    return r if r is not None or not use_dead or not blocked_edges() else _route(start, goal, {})


def _route(start, goal, dead):
    g = graph(); doors = gamedoors.load(); start, goal = int(start), int(goal)
    if start == goal: return []
    prev = {start: None}; queue = collections.deque([start])
    while queue:
        cur = queue.popleft()
        edges = [(nxt, ("side", side)) for side, nxt in zip(SIDES, g.get(cur, ())) if nxt and side not in dead.get(str(cur), ())]
        edges += [(int(d["to"]), ("door", d)) for d in doors.get(cur, ()) if d.get("to")]
        edges += border_edges(cur)
        for nxt, step in edges:
            if nxt in prev or nxt in tutorial.MAPS: continue      # the tutorial maps are closed behind a new character
            prev[nxt] = (cur, step)
            if nxt == goal:
                out = []
                while prev[nxt]: out.append(prev[nxt][1]); nxt = prev[nxt][0]
                return out[::-1]
            queue.append(nxt)
    return None


def distances(start, allowed=None):
    """BFS over maps (sides + learned doors): {map id: number of steps} from `start`. Blocked borders only count when nothing else is reachable.
    allowed: set of map ids the walk may cross (a zone): nothing outside it is visited."""
    d = _distances(start, blocked_edges(), allowed)
    return d if len(d) > 1 or not blocked_edges() else _distances(start, {}, allowed)


def _distances(start, dead, allowed=None):
    g = graph(); doors = gamedoors.load(); start = int(start)
    dist = {start: 0}; queue = collections.deque([start])
    while queue:
        cur = queue.popleft()
        nxts = [n for side, n in zip(SIDES, g.get(cur, ())) if n and side not in dead.get(str(cur), ())] + [int(d["to"]) for d in doors.get(cur, ()) if d.get("to")]
        nxts += [to for to, _s in border_edges(cur)]
        for n in nxts:
            if n not in dist and n not in tutorial.MAPS and (allowed is None or n in allowed): dist[n] = dist[cur] + 1; queue.append(n)
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
        self.s = sess; self.farm = None; self.explore = 0; self.discover_tries = collections.Counter(); self.on = False; self.goal = None; self.quest_id = None; self.kinds = {"talk", "fight", "craft"}; self.llm = False; self.llm_steps = 0; self._last_sync = 0.0; self.want_fight = False; self.fight_played = False; self._stuck = []; self._hinted = set()
        self._stop = threading.Event(); self._thread = None; self.attempts = collections.Counter(); self._location_maps = {}; self._start_tried = set(); self._new_quests = 0; self._prefer_words = set(); self._explored = set(); self._gear_skip = set(); self._incarnam_ids = None; self._escorts = set(); self._steps_taken = collections.deque(maxlen=40); self._danger = set(); self._worlds_tried = set(); self._incarnam_retried = False; self.help = None; self._focus_qid = None; self._equip_failed = set(); self._no_small_group_left = False; self._guide_said = None; self._guided_said = None; self.guided = False; self._guided_zaaps = set(); self._guided_visited = set(); self._guided_nozaap = set(); self._guided_swept = set(); self._guided_dip = None; self._level_said = set(); self._guided_given_up = set(); self._guided_skip = set(); self._guided_fail = collections.Counter(); self.guided = False; self._guided_skip = set(); self._guided_fail = collections.Counter()

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
        self.kinds = set(kinds or ("talk", "fight", "craft")); self.llm = bool(llm) and guide.available(); self.llm_steps = 0; self._hinted = set()
        self.s.resume_auto = (kinds, llm, quest_id)
        self.on = True; self.goal = None; self._stop = threading.Event(); self.attempts.clear(); self._start_tried = set(); self._new_quests = 0; self._worlds_tried = set(); self._incarnam_retried = False; self._explored = set(); self._escorts = set(); self._deaths0 = getattr(self.s, "deaths", 0)
        self._thread = threading.Thread(target=self._run, daemon=True); self._thread.start()
        return True

    def stop(self):
        self.s.resume_auto = None      # asked by the user: no restart after a reconnection
        self._stop.set()

    def say(self, level, msg):
        self.s.log(level, "Autopilote : " + msg)

    def _hunt_achievement(self, mons, ach, label, max_fights=150):
        """Kill the given monsters until the achievement is VALIDATED for this character (reached and claimed: mdw -> mdj). -> True once it is."""
        done = lambda: ach in self.s.achievements or ach in self.s.claimed_all
        fights = 0; visited = set(); failed = set()
        self.say("info", f"succès « {label} » : je chasse jusqu'à sa validation")
        while self._alive() and not done() and fights < max_fights:
            self._refresh_connection(); self._rest_hp()
            maxg = GEAR_MAX_GROUP + (1 if (self.s.level or 0) >= 10 else 0) + (1 if (self.s.level or 0) >= 12 else 0)
            kills = knowledge.load(f"kills_{self.s.char_id}.json", {})
            todo = {m for m in mons if not kills.get(str(m))} or set(mons)      # colours with no kill recorded yet come first
            group = self._best_group(todo, maxg) or self._best_group(set(mons), maxg)
            self.goal = f"succès {label} : {fights} combats ; sans kill enregistré : {sorted(todo) if len(todo) < len(mons) else 'toutes les couleurs'}"
            if group is not None:
                try: self._act(f"Chasse : groupe {group}", lambda: self.s._do_attack(group, max_group=maxg))
                except RuntimeError as e:
                    self.say("warn", f"combat impossible : {e}")
                    if "trop gros" in str(e): failed.add(self.s.map_id); continue
                    raise DesyncError("combat fantôme retenu par le serveur")
                fights += 1; time.sleep(REST_SECONDS)
                if fights % 2 == 0 or fights < 3: self.s.claim_achievements(wait=2.5)      # the reward is claimed by hand: the server then names what it validated
                continue
            dist = distances(self.s.map_id); visited.add(self.s.map_id)
            seen = [m for mon in mons for m in knowledge.load("monster_seen.json", {}).get(str(mon), [])]
            cands = [m for m in dict.fromkeys(seen) if m in dist and m not in visited and m not in failed]
            if not cands:
                visited.clear(); cands = [m for m in dict.fromkeys(seen) if m in dist and m not in failed]
                if not cands: self.say("warn", "aucune carte de Piou accessible"); break
            nxt = min(cands, key=lambda m: dist[m]); self.say("info", f"aucun groupe de Piou ici : je vais sur la carte {nxt}")
            if not self._travel(nxt, ask_help=False):
                failed.add(nxt)
                if len(failed) >= 6: self.say("warn", "trop de cartes inaccessibles"); break
            else: self._sleep(2.0)
        self.s.claim_achievements(wait=3.0)
        ok = done()
        self.say("info", f"succès « {label} » : {'validé' if ok else 'pas encore validé'} après {fights} combats")
        return ok

    def _wear_pieces(self, pieces):
        """Wear the pieces that are in the bag and that the character's level allows (the item's level is the minimum to wear it). -> number worn now."""
        worn = 0
        for g in pieces:
            if g in self.s.equipped or self.s.have(g) <= 0 or g in self._equip_failed or not self._alive(): continue
            need = (gamedata.get("items", g) or {}).get("level") or 0
            if self.s.level is not None and need > self.s.level:
                if (g, self.s.level) not in self._level_said: self._level_said.add((g, self.s.level)); self.say("info", f"« {gamejobs.item_name(g)} » demande le niveau {need} (je suis niveau {self.s.level}) : je le garde dans le sac pour plus tard")
                continue
            typ = (gamedata.get("items", g) or {}).get("type")
            for pos in GEAR_SLOTS.get(typ, ()):
                try: self._act(f"Équiper {gamejobs.item_name(g)}", lambda: self.s._do_equip(g, pos)); worn += 1; break
                except RuntimeError as e: self.say("warn", f"équipement impossible ({gamejobs.item_name(g)}, emplacement {pos}) : {e}")
            else: self._equip_failed.add(g)      # refused in every slot: not asked again this run (the same refused request after each fight is spam, and the server hung up around those)
        return worn

    def _gear_farm(self):
        """Once the Incarnam quests are over, farm the monsters of a low-level set (Piou Bleu) and wear what drops, before the Astrub quests.
        Done once per character (knowledge/gear_farm.json). -> True if it did something (the main loop then starts over)."""
        if self.quest_id is not None or not self.s.char_id: return False
        done = knowledge.load("gear_farm.json", {})
        for name, mon, pieces in GEAR_SETS:
            key = f"{self.s.char_id}:{name}"
            if done.get(key) or key in self._gear_skip: continue
            import questreport
            if self._incarnam_ids is None: self._incarnam_ids = set(questreport.area_quests("Incarnam"))
            with self.s._lock: act = set(self.s.quests)
            if act & self._incarnam_ids:       # every Incarnam quest first; the only exception: what is left is a fight left aside as too hard and nothing else can be done, the set comes first then
                if not self._incarnam_retried and self._retry_incarnam(): return False      # what was given up on gets one more round first
                if not self._danger or self._pick(any_world=True, only=self._incarnam_ids)[0] is not None: return False
            self._gear_skip.add(key)      # one attempt per run: when it ends early the Astrub quests go on
            have = lambda g: self.s.have(g) > 0 or g in self.s.equipped
            wear = lambda: self._wear_pieces(pieces)      # wear what dropped (after every fight: the pieces help in the next ones)
            fights = 0; visited = set(); failed = set(); self.say("info", f"panoplie « {name} » : chasse des {gamedata.quest_data('monsters', mon) or mon}")
            while self._alive() and not all(have(g) for g in pieces) and fights < MAX_GEAR_FIGHTS:
                self._refresh_connection()
                self._rest_hp()
                world = self._world(self.s.map_id); maps = [m for m in gamedata.monster_maps(mon) if self._world(m) not in (None, world)] if world else []
                if maps and self._change_world(self._world(maps[0])) is False: self.say("warn", "retour au monde de la chasse impossible"); break
                maxg = GEAR_MAX_GROUP + (1 if (self.s.level or 0) >= 10 else 0) + (1 if (self.s.level or 0) >= 12 else 0)      # three monsters killed the level 8 character: the limit grows with the level
                group = self._best_group({mon}, maxg); self.goal = f"panoplie {name} : {sum(have(g) for g in pieces)}/{len(pieces)} pièces"
                if group is not None:
                    self._fight_for("gear")
                    try: self._act(f"Chasse : groupe {group}", lambda: self.s._do_attack(group, max_group=maxg))
                    except RuntimeError as e:
                        self.say("warn", f"combat impossible : {e}")
                        if "trop gros" in str(e): failed.add(self.s.map_id); continue      # a refusal of ours, not a ghost fight: no reconnection
                        raise DesyncError("combat fantôme retenu par le serveur")      # the server holds the fight for minutes: reconnect (with long pauses) instead of stopping
                    fights += 1; wear(); time.sleep(REST_SECONDS); continue
                dist = distances(self.s.map_id); visited.add(self.s.map_id)
                seen = [m for m in knowledge.load("monster_seen.json", {}).get(str(mon), []) if m in dist and m not in visited and m not in failed]      # maps where this monster was really met first
                cands = seen or [m for m in gamedata.monster_maps(mon) if m in dist and m not in visited and m not in failed]
                with self.s._lock: big = [len(ms) for a_, ms in self.s.groups.items() if mon in ms]
                if big: self.say("info", f"groupes de Piou sur cette carte : {big} monstre(s) (limite {GEAR_MAX_GROUP + (1 if (self.s.level or 0) >= 10 else 0) + (1 if (self.s.level or 0) >= 12 else 0)})")
                if not cands: self.say("warn", f"aucune carte accessible où vivent les {gamedata.quest_data('monsters', mon)}"); break
                nxt = min(cands, key=lambda m: dist[m])
                self.say("info", f"aucun groupe de Piou ici : je vais sur la carte {nxt}")
                if not self._travel(nxt, ask_help=False):
                    failed.add(nxt)      # a map behind a door nobody opened / a border the server refuses: try the next one
                    if len(failed) >= MAX_GEAR_TRAVEL_FAILURES: self.say("warn", f"panoplie : {len(failed)} voyages échoués, je laisse la pièce manquante"); break
                    continue
                self._sleep(2.0)
            if self.s.status != "connected" and not self._stop.is_set(): self._gear_skip.discard(key)      # the connection dropped: resume the hunt after the reconnection
            wear()
            got = sum(have(g) for g in pieces)
            self.say("info", f"panoplie « {name} » : {got}/{len(pieces)} pièces après {fights} combats")
            if all(have(g) for g in pieces) or fights >= MAX_GEAR_FIGHTS:
                with knowledge._lock: d = knowledge.load("gear_farm.json", {}); d[key] = got; knowledge.save("gear_farm.json", d)
            if self._danger and got == len(pieces):      # fully equipped: the fights left aside are worth another try
                with knowledge._lock:
                    d = knowledge.load(self._kf("dangerous_objectives.json"), {})
                    for oid in self._danger: d.pop(str(oid), None); self.attempts[oid] = 0
                    knowledge.save(self._kf("dangerous_objectives.json"), d)
                self.say("info", f"{len(self._danger)} objectif(s) de combat remis en jeu avec la panoplie"); self._danger.clear()
            return True
        return False

    def _rest_hp(self, wanted=0.8, timeout=240):
        """Do not start the next objective (it may be a fight) with the life low: wait for it to regenerate (it only does so out of combat).
        The server does not push the regenerated life (the client computes it, about 1 HP per REGEN_SECONDS), so the wait is estimated from the last value received."""
        end = time.time() + timeout; announced = False
        while self._alive() and time.time() < end:
            with self.s._lock: v = self.s._vitals() or {}      # the session already estimates the regeneration from the last real value (login or end of a fight)
            hp, mx = v.get("hp"), v.get("hp_max")
            if hp is None or not mx: return
            if hp >= wanted * mx:
                if announced: self.say("info", f"vie rétablie ({hp}/{mx})")
                return
            if not announced: self.say("info", f"vie basse ({hp}/{mx}) : je me repose avant le prochain objectif"); announced = True
            time.sleep(min(10, max(1.0, REGEN_SECONDS)))

    def _note_deaths(self, oid, n):
        """Deaths while working on an objective, remembered in knowledge/dangerous_objectives.json: from MAX_OBJECTIVE_DEATHS the objective is left alone
        (the fight is too hard for this character), here and in later runs."""
        if n <= 0: return
        with knowledge._lock:
            d = knowledge.load(self._kf("dangerous_objectives.json"), {}); cur = _danger_entry(d.get(str(oid))); d[str(oid)] = {"n": cur["n"] + n, "level": self.s.level or 0}; knowledge.save(self._kf("dangerous_objectives.json"), d)
        total = d[str(oid)]["n"]
        self.say("warn", f"{total} défaite(s) sur l'objectif « {gamequests.objective_text(oid)} »")
        if total >= MAX_OBJECTIVE_DEATHS: self.attempts[oid] = 99; self._danger.add(oid); self.say("warn", "objectif mis de côté : combat trop dur pour ce personnage")

    def _alive(self):
        return not self._stop.is_set() and self.s.status == "connected"

    def _act(self, label, fn):
        """Run one atomic action under the session's action lock (shared with the UI buttons)."""
        self._refresh_connection()      # an action is a safe moment to renew a connection the server is about to hang up (never in a fight)
        if not self.s._act.acquire(timeout=30): raise RuntimeError("une autre action est en cours")
        try:
            with self.s._lock: self.s.busy = label
            self.s.log("action", label); fn()
        except DesyncError as e:
            with self.s._lock: self.s.busy = None
            self.s._act.release()
            self.say("warn", "position désynchronisée : reconnexion")
            try: self.s.reconnect(); e.reconnected = True
            finally: self.s._act.acquire(timeout=60)
            raise
        finally:
            with self.s._lock: self.s.busy = None
            try: self.s._act.release()
            except RuntimeError: pass

    def _kf(self, name):
        """Per-character knowledge file (objectives left aside, defeats...): a new character must not inherit what the previous one learned about itself."""
        return f"{name[:-5]}_{self.s.char_id}.json" if self.s.char_id else name

    # ---- main loop ----
    def _run(self):
        self.say("info", "démarré")
        for oid, ts in knowledge.load(self._kf(GATHER_FAILED_FILE), {}).items():
            if time.time() - ts < GATHER_RETRY_HOURS * 3600: self.attempts[int(oid)] = 99; self.say("info", f"objectif « {gamequests.objective_text(int(oid))} » laissé de côté (ressource introuvable il y a peu)")
        for oid, text in knowledge.load(self._kf(UNVALIDATED_FILE), {}).items(): self.attempts[int(oid)] = 99; self.say("info", f"objectif « {text} » ignoré (jamais validé lors des runs précédents)")
        self._danger = set(); lvl = self.s.level or 0
        with knowledge._lock:
            d = knowledge.load(self._kf("dangerous_objectives.json"), {}); changed = False
            for oid, raw in list(d.items()):
                e = _danger_entry(raw)
                if e["level"] and lvl >= e["level"] + DANGER_FORGIVE_LEVELS:      # the character grew since: the fight may be easy now
                    del d[oid]; changed = True; self.say("info", f"objectif « {gamequests.objective_text(int(oid))} » : de nouveau tenté (niveau {lvl}, défaites au niveau {e['level']})"); continue
                if e["n"] >= MAX_OBJECTIVE_DEATHS: self.attempts[int(oid)] = 99; self._danger.add(int(oid)); self.say("info", f"objectif « {gamequests.objective_text(int(oid))} » ignoré ({e['n']} défaites passées)")
            if changed: knowledge.save(self._kf("dangerous_objectives.json"), d)
        try:
            drops = collections.deque()      # times of the recent drops: MAX_RECONNECTS counts the ones of the last RECONNECT_WINDOW seconds, a long run survives many isolated drops
            while True:
                now = time.time()
                while drops and now - drops[0] > RECONNECT_WINDOW: drops.popleft()
                reconnects = len(drops)
                try:
                    while self._alive():
                        if self.s.in_fight:   # a fight already running (reconnection, manual start): play its turns instead of stopping
                            self.say("info", "un combat est en cours : je le reprends")
                            try: self._act("Reprise du combat", self.s._do_resume_fight)
                            except RuntimeError as e: self.say("warn", f"combat impossible : {e}"); raise DesyncError("combat fantôme retenu par le serveur")      # the server holds the fight for minutes: reconnect (with long pauses) instead of stopping
                            time.sleep(REST_SECONDS); continue
                        if self.s.map_id in tutorial.MAPS and tutorial.step(self): continue      # a new character starts in the tutorial
                        if self.s.dialog: self._dialog(); continue
                        if self._dungeon_step(): continue
                        gtarget = self._guided_target() if self.guided and self.quest_id is None else None      # guided mode: the rush guide, to the letter
                        if self.guided and self.quest_id is None and gtarget is None: self.say("info", "guide de rush : parties 1 à 3 terminées"); self._stop.set(); continue
                        if gtarget is None and self.quest_id is None and self._start_new_quests(incarnam_only=True): continue      # the Incarnam quests not begun yet come before anything else, the set included
                        if gtarget is None and self._gear_farm(): continue
                        obj = None
                        if gtarget is not None:
                            obj, acted = self._guided_pick(gtarget)
                            if acted or obj is None: time.sleep(0.5); continue
                        elif self.quest_id is None:      # the Incarnam quests still open come first, wherever the character is (the portal takes it back)
                            import questreport
                            if self._incarnam_ids is None: self._incarnam_ids = set(questreport.area_quests("Incarnam"))
                            with self.s._lock: open_inc = bool(set(self.s.quests) & self._incarnam_ids)
                            if open_inc: obj, why = self._pick(any_world=True, only=self._incarnam_ids)
                        if obj is None and self.quest_id is None:      # the rush guide gives the order: the first quest it names that is active now, wherever it is
                            with self.s._lock: active = set(self.s.quests); fin = set(self.s.finished_quests)
                            gids, gstep, gname = guideorder.next_active(active, fin)
                            if gids:
                                obj, why = self._pick(any_world=True, only=gids)
                                if obj is not None and self._guide_said != gstep: self._guide_said = gstep; self.say("info", f"guide de rush, étape {gstep} : « {gname} »")
                        if obj is None: obj, why = self._pick()
                        if obj is None and not self._incarnam_retried and self._retry_incarnam(): continue      # before any trip to another world
                        if obj is None: obj, why2 = self._pick(any_world=True); why = why if obj is None else why2       # nothing left here: the other world (back through the portal)
                        if obj is None and time.time() - self._last_sync > 20:   # the server does not push the next step by itself: ask for the fresh state, once in a while
                            self._last_sync = time.time()
                            try: self.s.sync_quests()
                            except Exception: pass
                            time.sleep(0.5); obj, why = self._pick()
                        if obj is None and "aucune route" in (why or "") and self._explore_borders(): continue       # a way out nobody has used yet (interiors)
                        if obj is None and self.quest_id is None and self._start_new_quests(): continue
                        if obj is None and self.quest_id is None and self._next_world(): continue       # the starting zone has nothing left: the quests go on in the next world (Astrub)
                        if obj is None:
                            if self.llm and self._unblock(): continue
                            if self.quest_id is None and self.llm and self.llm_steps < MAX_LLM_STEPS and self._ask_guide(why): continue
                            self.say("info", why); break
                        o = gamedata.quest_data('objectives', obj[1]) or {}
                        tw = self._world(int(o["map"])) if o.get("map") else self._fight_world(o) if o.get("type") in FIGHT_TYPES else None
                        if tw and self.s.map_id and tw != self._world(self.s.map_id):          # the objective is in another world: take the portal first
                            if not self._change_world(tw):
                                self.attempts[obj[1]] += 1; self.say("warn", f"impossible de rejoindre le monde « {tw} »"); time.sleep(1.0); continue
                        self._rest_hp()
                        deaths0 = getattr(self.s, "deaths", 0)
                        done = self._fight_objective(*obj) if o.get('type') in FIGHT_TYPES else self._objective(*obj)
                        self._note_deaths(obj[1], getattr(self.s, "deaths", 0) - deaths0)
                        if not done:
                            self.say("info", f"objectif « {gamequests.objective_text(obj[1])} » abandonné" + ("" if self._alive() else " (arrêt demandé ou déconnexion)"))
                            if not self._alive() and not self._stop.is_set(): self.attempts[obj[1]] += max(MAX_ATTEMPTS, FREE_ATTEMPTS) // 2      # the connection dropped on it: two drops and it is left aside, it may be what kicks us
                            if o.get('type') == CRAFT_TYPE and self._alive(): time.sleep(0.5); continue   # a craft that lacks a drop must not stop the other quests (_pick skips it after MAX_ATTEMPTS)
                            if self.llm and self._alive():      # the bot gave up on it: maybe Groq knows where it really is
                                self._stuck = [obj]
                                if self._unblock(): continue
                            if self._alive() and self.attempts[obj[1]] > 0: time.sleep(0.5); continue      # the other objectives may unlock it (a quest needing two items); _pick skips this one once its tries are used
                            break
                        time.sleep(0.5)
                except DesyncError as e:      # the server refuses every move: our position is stale, a new connection tells the real one
                    self.say("warn", f"{e} : reconnexion ({reconnects + 1}/{MAX_RECONNECTS})")
                    if reconnects >= MAX_RECONNECTS or not self.s.reconnect(): self.say("error", "reconnexion impossible"); break
                    drops.append(time.time()); continue
                if self._stop.is_set() or self.s.status == "connected": break       # a normal end
                if reconnects >= MAX_RECONNECTS: self.say("error", f"connexion perdue {MAX_RECONNECTS} fois : arrêt"); break
                self.say("warn", f"connexion perdue : je me reconnecte et je reprends ({reconnects + 1}/{MAX_RECONNECTS})")
                drops.append(time.time())
                if not self.s.reconnect(): self.say("error", "reconnexion impossible"); break
        except Exception as e:
            import traceback; tb = traceback.extract_tb(e.__traceback__)[-1]
            self.say("error", f"{type(e).__name__}: {e} ({os.path.basename(tb.filename)}:{tb.lineno} {tb.line})")
        finally:
            self.on = False; self.goal = None; self.say("info", "arrêté")

    # ---- guided mode: the rush guide to the letter (guidesteps.py) ----
    def _guided_progress(self, mark=None):
        path = self._kf("rush_progress.json")
        with knowledge._lock:
            d = knowledge.load(path, {"done": []})
            if mark is not None and mark not in d["done"]: d["done"].append(mark); knowledge.save(path, d)
        return set(d["done"])

    def _guided_target(self):
        """The first step of the guide that is not done -> step dict, or None when the guide (parts 1-3) is over."""
        done = self._guided_progress()
        for st in guidesteps.steps():
            if st["n"] not in done: return st
        return None

    def _guided_halt(self, st, why):
        self.say("warn", f"guide de rush, étape {st['n']} (partie {st['part']}) : {why} — je m'arrête ici (rien d'autre n'est lancé). Texte du guide : {st['text']}")
        self.goal = f"guide, étape {st['n']} : {why}"; self._stop.set(); return None, True

    def _guided_quest_id(self, st):
        if st.get("class_devotion"):
            breed = gamedata.get("breeds", self.s.breed_id) if self.s.breed_id else None
            name = f"Dévotion à {breed}" if breed else None
        else: name = st.get("name")
        ids = guideorder._names().get(guideorder.norm(name or "")) or []
        return (ids[0] if ids else None), name

    def _guided_pick(self, st):
        """-> (objective or None, acted). One guide step at a time; a step the robot cannot do stops the run instead of being skipped."""
        n, kind = st["n"], st["kind"]
        self._guided_hint = st["text"] + " " + st.get("chall", "")      # the text of the step: it may ask for a challenge ("chall drop", "succès prudent"...), see challenges.parse_hint
        if self._guided_said != n: self._guided_said = n; self.say("info", f"guide de rush, étape {n}/{len(guidesteps.RAW)} (partie {st['part']}) : {st['text']}")
        if kind == "auto":
            if st.get("cond") == "tutorial" and self.s.map_id in tutorial.MAPS: return None, True
            self._guided_progress(n); return None, True
        if kind == "manual": return self._guided_halt(st, "étape manuelle, pas encore automatisée")
        if kind == "goto":
            cands = guidesteps.maps_at(st["x"], st["y"], self._world(self.s.map_id) or "Monde des Douze")
            if self.s.map_id in cands: self._guided_progress(n); return None, True
            reach = distances(self.s.map_id) if self.s.map_id else {}
            ok = [m for m in cands if m in reach]
            if not ok: return self._guided_halt(st, f"aucune carte atteignable en {st['x']},{st['y']}")
            if not self._travel(min(ok, key=lambda m: reach[m])): self._guided_fail[n] += 1
            if self._guided_fail[n] >= 4: return self._guided_halt(st, "déplacement impossible")
            return None, True
        if kind == "zaap":
            maps = st.get("maps")
            if st.get("sweep"):      # no known place: walk the maps of these subareas, nearest first, until one has a zaap element
                norm = guideorder.norm; subs = {norm(x) for x in st["sweep"]}
                if any(m in self._guided_zaaps for m in self._guided_swept): self._guided_progress(n); return None, True
                reach = distances(self.s.map_id) if self.s.map_id else {}
                cand = [int(m) for m in gamedata.data()["maps"] if int(m) in reach and (gamedata.map_info(m) or {}).get("subarea") and norm(gamedata.map_info(m)["subarea"]) in subs and int(m) not in self._guided_swept]
                if not cand: return self._guided_halt(st, f"aucun zaap trouvé après {len(self._guided_swept)} cartes parcourues")
                tgt = min(cand, key=lambda m: reach[m])
                if self.s.map_id != tgt:
                    if not self._travel(tgt): self._guided_swept.add(tgt)
                    return None, True
                self._guided_swept.add(tgt)
                with self.s._lock: zaaps = [k for k, v in self.s.elements.items() if v.get("type") == 114]
                if zaaps:
                    try: self._act(f"Utiliser le zaap {zaaps[0]}", lambda: self.s._do_use(zaaps[0], until=lambda: True)); time.sleep(1.5); self.s._send("kiq")
                    except RuntimeError as e: self.say("warn", f"zaap : {e}"); return None, True
                    self._guided_zaaps.add(tgt); self.say("event", f"zaap de Brâkmar trouvé et utilisé (carte {tgt})"); self._guided_progress(n)
                return None, True
            if maps is None:      # coordinates: the maps of that square in the area; each is checked for a zaap element
                maps = [m for m in guidesteps.maps_at(*st["xy"]) if (gamedata.map_info(m) or {}).get("area") and guideorder.norm(gamedata.map_info(m)["area"]) == guideorder.norm(st["area"])]
                if not maps: return self._guided_halt(st, f"aucune carte en {st['xy']}")
            todo = [m for m in maps if m not in self._guided_zaaps and m not in self._guided_nozaap]
            if not todo:
                if st.get("xy") and not any(m in self._guided_zaaps for m in maps): return self._guided_halt(st, f"aucun zaap sur les cartes en {st['xy']}")
                self._guided_progress(n); return None, True
            tgt = todo[0]
            if self.s.map_id != tgt:
                if not self._travel(tgt):
                    self._guided_fail[n] += 1
                    if self._guided_fail[n] >= 4: return self._guided_halt(st, f"carte {tgt} (zaap) inaccessible")
                return None, True
            with self.s._lock: zaaps = [k for k, v in self.s.elements.items() if v.get("type") == 114]
            if not zaaps:
                if st.get("xy"): self._guided_nozaap.add(tgt); return None, True      # not this map of the square: try the next one
                return self._guided_halt(st, f"pas de zaap sur la carte {tgt}")
            try: self._act(f"Utiliser le zaap {zaaps[0]}", lambda: self.s._do_use(zaaps[0], until=lambda: True))
            except RuntimeError as e: self.say("warn", f"zaap : {e}"); self._guided_fail[n] += 1
            else:
                time.sleep(1.5)
                try: self.s._send("kiq")      # close the zaap menu, as the client does
                except RuntimeError: pass
                self._guided_zaaps.add(tgt); self.say("event", f"zaap de la carte {tgt} utilisé")
            if self._guided_fail[n] >= 4: return self._guided_halt(st, "le zaap ne répond pas")
            return None, True
        if kind == "gear":
            if self.s.map_id in tutorial.MAPS: return None, True
            sets = [x for x in GEAR_SETS if x[0] == st["set"]]
            if not sets: return self._guided_halt(st, f"panoplie « {st['set']} » inconnue")
            self._incarnam_ids = set()      # the guide decides the order: the Incarnam quests do not hold the farm back
            self._gear_skip.discard(f"{self.s.char_id}:{st['set']}")
            with knowledge._lock: d = knowledge.load("gear_farm.json", {}); key = f"{self.s.char_id}:{st['set']}"; d.pop(key, None); knowledge.save("gear_farm.json", d)
            if st.get("achievement") and (st["achievement"] in self.s.achievements or st["achievement"] in self.s.claimed_all) and self.s.have(sets[0][2][0]) + self.s.have(sets[0][2][1]) >= 0 and all(self.s.have(g) > 0 or g in self.s.equipped for g in sets[0][2]):
                self._wear_pieces(sets[0][2]); self._guided_progress(n); return None, True      # set complete and achievement validated
            self._guided_fail[n] += 1
            if self._guided_fail[n] > 3 and not st.get("achievement"): self._wear_pieces(sets[0][2]); self._guided_progress(n); return None, True      # farm run ends: what is in the bag and allowed is worn
            self._gear_farm(); self._wear_pieces(sets[0][2])
            with knowledge._lock: done = knowledge.load("gear_farm.json", {}).get(f"{self.s.char_id}:{st['set']}")
            if done is not None:
                if st.get("achievement") and not self._hunt_achievement(set(st["monsters"]), st["achievement"], st["achievement_name"]):
                    if self._guided_fail[n] >= 3: return self._guided_halt(st, f"succès « {st['achievement_name']} » pas validé")
                    return None, True
                self._guided_progress(n)
            return None, True
        if kind == "dungeon":
            norm = guideorder.norm
            inf = gamedata.map_info(self.s.map_id) if self.s.map_id else None
            inside = bool(inf and inf["subarea"] and norm(st["subarea"]) in norm(inf["subarea"]))
            if inside:
                if self._guided_dip != "in": self._guided_dip = "in"; self.say("event", f"guide de rush : entré dans « {inf['subarea']} »")
                xa = next((a for i in st.get("exit_npc_ids", []) for a in [self._find_npc(i)] if a is not None), None)
                if xa is not None:      # the boss (Kardorim) lets the player out with a reply
                    try:
                        self._act(f"Parler au PNJ {xa}", lambda: self.s._do_talk(xa))
                        dlg = self.s.dialog; reps = dlg.get("replies", []) if dlg else []
                        pick = next((r for r in reps if any(w in (r["text"] or "").casefold() for w in st["exit_words"])), None)
                        if pick is None:
                            self.say("warn", f"guide de rush : aucune réponse avec {st['exit_words']} parmi {[r['text'][:60] for r in reps]}"); self.s.close_dialog(); self._guided_fail[n] += 1
                        else: self.say("info", f"guide de rush : réponse « {pick['text']} »"); self._act(f"Répondre ({pick['id']})", lambda: self.s._do_reply(pick["id"]))
                    except RuntimeError as e: self.say("warn", f"guide de rush : {e}"); self._guided_fail[n] += 1
                    if self._guided_fail[n] >= 4: return self._guided_halt(st, "Kardorim ne me laisse pas sortir")
                    self._sleep(2.5); return None, True
                ok = self.s.probe_doors()      # no monster group left in this room (the fights are _dungeon_step's): the way on / out is a door
                if not ok:
                    self._guided_fail[(n, self.s.map_id)] += 1
                    if self._guided_fail[(n, self.s.map_id)] >= 4: return self._guided_halt(st, f"je ne trouve pas la suite dans « {inf['name']} » (carte {self.s.map_id})")
                return None, True
            if self._guided_dip == "in": self._guided_dip = None; self._guided_progress(n); self.say("event", "guide de rush : donjon terminé, je suis ressorti"); return None, True
            if self.s.map_id != st["entry"]:
                if not self._travel(st["entry"]):
                    self._guided_fail[n] += 1
                    if self._guided_fail[n] >= 3: return self._guided_halt(st, f"carte d'entrée {st['entry']} inaccessible")
                return None, True
            actor = next((a for i in st.get("npc_ids", []) for a in [self._find_npc(i)] if a is not None), None)
            if actor is not None:      # the gatekeeper (Klasmor) opens the crypt with a reply
                try:
                    self._act(f"Parler au PNJ {actor}", lambda: self.s._do_talk(actor))
                    for _ in range(6):
                        dlg = self.s.dialog
                        if not dlg: break
                        reps = dlg.get("replies", [])
                        pick = next((r for r in reps if any(w in (r["text"] or "").casefold() for w in st["words"])), None)
                        if pick is None:
                            self.say("warn", f"guide de rush : aucune réponse avec {st['words']} parmi {[r['text'][:60] for r in reps]}"); self.s.close_dialog(); self._guided_fail[n] += 1; break
                        self.say("info", f"guide de rush : réponse « {pick['text']} »"); self._act(f"Répondre ({pick['id']})", lambda: self.s._do_reply(pick["id"])); time.sleep(1.5)
                except RuntimeError as e: self.say("warn", f"guide de rush : {e}"); self._guided_fail[n] += 1
                if self._guided_fail[n] >= 3: return self._guided_halt(st, "le gardien de la crypte ne répond pas comme prévu")
                self._sleep(2.5); return None, True
            self.say("info", f"guide de rush : je cherche la porte de « {st['subarea']} » sur la carte {st['entry']}")
            if not self.s.probe_doors():
                self._guided_fail[n] += 1
                if self._guided_fail[n] >= 3: return self._guided_halt(st, f"porte de « {st['subarea']} » introuvable")
            return None, True
        if kind == "dip":
            inf = gamedata.map_info(self.s.map_id) if self.s.map_id else None
            inside = bool(inf and inf["subarea"] and guideorder.norm(st["subarea"]) in guideorder.norm(inf["subarea"]))
            if self._guided_dip is None:
                if inside: self._guided_dip = "in"      # started inside: just get out
                elif self.s.map_id != st["map"]:
                    if not self._travel(st["map"]):
                        self._guided_fail[n] += 1
                        if self._guided_fail[n] >= 3: return self._guided_halt(st, f"carte {st['map']} inaccessible")
                    return None, True
                else:
                    self.say("info", f"guide de rush : je cherche le passage vers « {st['subarea']} » sur la carte {st['map']}")
                    ok = self.s.probe_doors()
                    inf2 = gamedata.map_info(self.s.map_id) if self.s.map_id else None
                    if ok and inf2 and guideorder.norm(st["subarea"]) in guideorder.norm(inf2["subarea"] or ""): self._guided_dip = "in"; self.say("event", f"guide de rush : entré dans « {inf2['subarea']} »"); return None, True
                    self._guided_fail[n] += 1
                    if self._guided_fail[n] >= 3: return self._guided_halt(st, "passage vers les égouts introuvable")
                    return None, True
            if self._guided_dip == "in":
                if not inside and self.s.map_id == st["map"]: self._guided_progress(n); self._guided_dip = None; self.say("event", "guide de rush : remonté des égouts"); return None, True
                if not inside: self._guided_progress(n); self._guided_dip = None; return None, True
                ok = self.s.probe_doors()      # the way back is a door / stairs near where we arrived
                if not ok:
                    self._guided_fail[n] += 1
                    if self._guided_fail[n] >= 4: return self._guided_halt(st, "je ne trouve pas la sortie des égouts")
                return None, True
        if kind == "explore":
            norm = guideorder.norm
            inf = gamedata.map_info(self.s.map_id) if self.s.map_id else None
            visited = self._guided_visited
            if inf and inf["subarea"] and any(norm(inf["subarea"]) == norm(x) for x in st["subareas"]) and inf["subarea"] not in visited:
                visited.add(inf["subarea"]); self.say("event", f"guide de rush : quartier « {inf['subarea']} » visité ({len(visited)}/{len(st['subareas'])})")
            if st.get("zaap") and self.s.map_id not in self._guided_zaaps:      # a zaap on the way: learn it
                with self.s._lock: zaaps = [k for k, v in self.s.elements.items() if v.get("type") == 114]
                if zaaps:
                    self._guided_zaaps.add(self.s.map_id)
                    try:
                        self._act(f"Utiliser le zaap {zaaps[0]}", lambda: self.s._do_use(zaaps[0], until=lambda: True)); time.sleep(1.5); self.s._send("kiq"); self.say("event", f"zaap de la carte {self.s.map_id} utilisé")
                    except RuntimeError as e: self.say("warn", f"zaap : {e}")
                    return None, True
            left = [x for x in st["subareas"] if not any(norm(x) == norm(v) for v in visited) and x not in self._guided_given_up]
            if not left:
                miss = [x for x in st["subareas"] if x in self._guided_given_up]
                if miss: return self._guided_halt(st, f"quartiers non atteints : {miss}")
                self._guided_progress(n); return None, True
            target = left[0]
            cands = [int(m) for m in gamedata.data()["maps"] if (gamedata.map_info(m) or {}).get("subarea") and norm(gamedata.map_info(m)["subarea"]) == norm(target)]
            reach = distances(self.s.map_id) if self.s.map_id else {}
            ok = [m for m in cands if m in reach]
            if not ok:
                cx, cy = (inf["x"], inf["y"]) if inf else (0, 0)
                near = sorted(cands, key=lambda m: max(abs(gamedata.map_info(m)["x"] - cx), abs(gamedata.map_info(m)["y"] - cy)))[:6]
                self.say("info", f"guide de rush : « {target} » : aucune route connue, je cherche la porte de la ville")
                found = any(self._can_discover(g) and self._discover(g) for g in near)
                if not found or not any(m in distances(self.s.map_id) for m in cands):
                    self._guided_fail[(n, target)] += 1
                    if self._guided_fail[(n, target)] >= 3: self._guided_given_up.add(target); self.say("warn", f"guide de rush : « {target} » introuvable, je le laisse")
                return None, True
            tgt = min(ok, key=lambda m: reach[m])
            if not self._travel(tgt):
                self._guided_fail[(n, target)] += 1
                if self._guided_fail[(n, target)] >= 3: self._guided_given_up.add(target); self.say("warn", f"guide de rush : « {target} » inaccessible, je le laisse")
            return None, True
        if kind == "talk":
            inf = gamedata.map_info(self.s.map_id) if self.s.map_id else None
            if inf and st.get("arrive_area") and inf["area"] == st["arrive_area"]: self._guided_progress(n); return None, True      # the NPC took us there
            actor = next((a for i in st["npc_ids"] for a in [self._find_npc(i)] if a is not None), None) if self.s.map_id != st["map"] or not st.get("door") else None
            if actor is None:
                if self.s.map_id != st["map"] and not st.get("door"):
                    if not self._travel(st["map"]):
                        self._guided_fail[n] += 1
                        if self._guided_fail[n] >= 4: return self._guided_halt(st, f"carte {st['map']} inaccessible")
                    return None, True
                if st.get("door") and self.s.map_id == st["map"]:      # the NPC is behind the passage of this map
                    try: self._act(f"Prendre le passage {st['door']}", lambda: self.s._do_use(st["door"]))
                    except RuntimeError as e:
                        self.say("warn", f"guide de rush : passage : {e}"); self._guided_fail[n] += 1
                        if self._guided_fail[n] >= 4: return self._guided_halt(st, "le passage ne s'ouvre pas")
                    self._sleep(2.0); return None, True
                if st.get("door") and self.s.map_id != st["map"]:      # past the passage: the NPC is on this map
                    actor = next((a for i in st["npc_ids"] for a in [self._find_npc(i)] if a is not None), None)
                    if actor is None:
                        if not self._travel(st["map"]):
                            self._guided_fail[n] += 1
                            if self._guided_fail[n] >= 4: return self._guided_halt(st, "PNJ introuvable derrière le passage")
                        return None, True
                else: return self._guided_halt(st, "PNJ introuvable sur la carte")
            try:
                self._act(f"Parler au PNJ {actor}", lambda: self.s._do_talk(actor))
                for _ in range(6):
                    dlg = self.s.dialog
                    if not dlg: break
                    reps = dlg.get("replies", [])
                    pick = next((r for r in reps if any(w in (r["text"] or "").casefold() for w in st["words"])), None)
                    if pick is None:
                        self.say("warn", f"guide de rush : aucune réponse avec {st['words']} parmi {[r['text'][:50] for r in reps]}"); self.s.close_dialog(); self._guided_fail[n] += 1; break
                    self.say("info", f"guide de rush : réponse « {pick['text']} »"); self._act(f"Répondre ({pick['id']})", lambda: self.s._do_reply(pick["id"])); time.sleep(1.5)
            except RuntimeError as e: self.say("warn", f"guide de rush : {e}"); self._guided_fail[n] += 1
            if self._guided_fail[n] >= 4: return self._guided_halt(st, "le PNJ ne répond pas comme prévu")
            self._sleep(2.5); return None, True
        if kind == "route":
            cur = self.s.map_id
            if cur == st["end"]: self._guided_progress(n); return None, True
            if cur in st["hops"]:
                actor, rid = st["hops"][cur]
                try:
                    self._act(f"Parler au PNJ {actor}", lambda: self.s._do_talk(actor))
                    if not self.s.dialog: raise RuntimeError("pas de dialogue")
                    self._act(f"Répondre ({rid})", lambda: self.s._do_reply(rid))
                except RuntimeError as e:
                    self.say("warn", f"guide de rush : {e}"); self._guided_fail[n] += 1
                    if self._guided_fail[n] >= 4: return self._guided_halt(st, f"le PNJ {actor} ne répond pas comme prévu")
                self._sleep(2.5); return None, True
            reach = distances(cur) if cur else {}
            goals = [m for m in list(st["hops"]) + [st["end"]] if m in reach]
            if not goals: return self._guided_halt(st, f"aucune route connue de la carte {cur} vers le trajet du guide")
            tgt = min(goals, key=lambda m: reach[m])
            if not self._travel(tgt):
                self._guided_fail[n] += 1
                if self._guided_fail[n] >= 4: return self._guided_halt(st, f"déplacement vers la carte {tgt} impossible")
            return None, True
        qid, name = self._guided_quest_id(st)       # quest
        if qid is None: return self._guided_halt(st, f"quête « {name} » introuvable dans les données du jeu")
        with self.s._lock: active = qid in self.s.quests; fin = qid in self.s.finished_quests
        if fin: self._guided_progress(n); return None, True
        if not active:
            try: self.s.sync_quests()
            except Exception: pass
            with self.s._lock:
                active = qid in self.s.quests; fin = qid in self.s.finished_quests
            if fin: self._guided_progress(n); return None, True
        if not active:
            if st.get("take"):
                if not self._start_guided(qid, name):
                    self._guided_fail[n] += 1
                    if self._guided_fail[n] >= 3: return self._guided_halt(st, f"« {name} » ne démarre pas")
                return None, True
            self._guided_fail[n] += 1       # a chain quest: it starts by itself when the previous one ends
            if self._guided_fail[n] >= 4: return self._guided_halt(st, f"« {name} » n'est pas apparue toute seule")
            time.sleep(3); return None, True
        if st.get("accept"): self.say("info", f"guide de rush : « {name} » acceptée (ses objectifs sont les quêtes des étapes suivantes)"); self._guided_progress(n); return None, True
        obj, why = self._pick(any_world=True, only={qid})
        if obj is None:
            try: self.s.sync_quests()
            except Exception: pass
            with self.s._lock: gone = qid not in self.s.quests
            if gone: return None, True
            if "aucune route" in (why or "") and self.s.probe_doors(): self.say("info", "guide de rush : pas de route connue — j'ai essayé les portes de cette carte"); return None, True      # stuck in a closed place (a class temple): its exit is a door to learn
            self._guided_fail[n] += 1
            if self._guided_fail[n] >= 6: return self._guided_halt(st, f"« {name} » bloquée : {why}")
            return None, True
        return obj, False

    def _start_guided(self, qid, name):
        """Go to the quest's starter NPC and talk to him, whatever the level. -> True if the quest is now active (or already finished)."""
        import questreport
        st = guideorder.STARTERS.get(qid) or questreport.starter(name)
        if not st: self.say("warn", f"guide de rush : PNJ de départ de « {name} » introuvable"); return False
        npc, ids, maps = st
        reach = distances(self.s.map_id) if self.s.map_id else {}
        tgt = self.s.map_id if self.s.map_id in maps else min(maps, key=lambda m: reach.get(m, 10 ** 6))
        tw = self._world(tgt)
        if tw and self.s.map_id and tw != self._world(self.s.map_id):
            self.say("info", f"guide de rush : « {name} » commence dans le monde « {tw} »")
            if not self._change_world(tw): self.say("warn", f"impossible de rejoindre le monde « {tw} »"); return False
        self.say("info", f"guide de rush : je vais parler à {npc} (carte {tgt}) pour « {name} »"); self.goal = f"guide : « {name} » auprès de {npc}"
        if self.s.map_id != tgt and not self._travel(tgt): return False
        actor = next((a for i in ids for a in [self._find_npc(i)] if a is not None), None)
        if actor is None and self.s.probe_doors():      # the NPC is inside a building of this map: the door found is learned, look again behind it
            self.say("info", f"guide de rush : porte trouvée sur la carte {tgt}, je cherche {npc} derrière")
            self._sleep(2.0); actor = next((a for i in ids for a in [self._find_npc(i)] if a is not None), None)
            if actor is not None: self.say("info", f"{npc} trouvé sur la carte {self.s.map_id} : à retenir comme carte de départ")
        if actor is None: self.say("warn", f"{npc} introuvable sur la carte {tgt}"); return False
        try: self._act(f"Parler au PNJ {actor}", lambda: self.s._do_talk(actor))
        except RuntimeError as e: self.say("warn", f"dialogue impossible : {e}"); return False
        self._dialog(); time.sleep(2.0)
        try: self.s.sync_quests()
        except Exception: pass
        with self.s._lock: ok = qid in self.s.quests or qid in self.s.finished_quests
        if ok: self.say("event", f"quête « {name} » démarrée")
        else: self.say("warn", f"« {name} » n'a pas démarré après le dialogue avec {npc}")
        return ok

    def _fight_for(self, purpose, **kw):
        """Tell the session what the next fight is for: it decides the challenge bonus (XP or loot, client/challenges.py), together with the text of the
        rush-guide step being run in guided mode."""
        self.s.fight_ctx = challenges.Context(purpose, getattr(self, "_guided_hint", "") if self.guided else "", **kw)

    def _dungeon_step(self):
        """Inside a dungeon (no way out on foot, name says crypt / tomb / dungeon) the way on is through the fights: the bot cannot walk out.
        Attack the closest monster group, whatever its size (dungeons are built that way). -> True if a fight was started."""
        info = gamedata.map_info(self.s.map_id) if self.s.map_id else None
        if not info or not any(w in (info["subarea"] or "").casefold() for w in DUNGEON_WORDS): return False
        with self.s._lock: groups = {a: list(ms) for a, ms in self.s.groups.items()}; actors = dict(self.s.actors); me = self.s.my_cell
        if not groups or me is None: return False
        if getattr(self.s, "deaths", 0) - getattr(self, "_deaths0", 0) >= MAX_DEFEATS:
            self.say("error", f"{MAX_DEFEATS} défaites dans « {info['subarea']} » : j'arrête plutôt que de recommencer (tactique à revoir)"); self._stop.set(); return False
        from fightbot import dist as cell_dist
        actor = min(groups, key=lambda a: cell_dist(me, actors.get(a, me)))
        self.say("info", f"donjon « {info['subarea']} » : j'attaque le groupe {actor} ({len(groups[actor])} monstre(s))")
        self._fight_for("dungeon")
        try: self._act(f"Donjon : combat contre le groupe {actor}", lambda: self.s._do_attack(actor, max_group=99))
        except RuntimeError as e: self.say("warn", f"combat de donjon impossible : {e}"); return False
        time.sleep(REST_SECONDS)
        return True

    def _world(self, map_id):
        mi = gamedata.map_info(map_id) if map_id else None
        return mi["world"] if mi else None

    def _pick(self, any_world=False, only=None):
        """-> ((quest id, objective id), None) or (None, reason). Only objectives of the zone we are in (the starting zone, Astrub...); among
        them the closest one first (steps from the current map), so the walking is as short as possible."""
        unsupported = None; cands = []; self._stuck = []
        def block(qid, oid, msg):
            nonlocal unsupported
            self._stuck.append((qid, oid)); unsupported = msg if unsupported is None else unsupported + (" ; " + msg if unsupported.count(" ; ") < 5 else "")
        with self.s._lock: quests = {q: (st, dict(objs)) for q, (st, objs) in self.s.quests.items()}
        if self.quest_id is not None and self.quest_id not in quests:
            return None, "la quête sélectionnée est terminée ou n'est plus active"
        here = self._world(self.s.map_id); reach = distances(self.s.map_id) if self.s.map_id else {}
        for qid, (_step, objs) in quests.items():
            if self.quest_id is not None and qid != self.quest_id: continue
            if only is not None and qid not in only: continue
            for oid, active in objs.items():
                if not active: continue
                o = gamedata.quest_data("objectives", oid) or {}
                kind = "talk" if o.get("type") in TALK_TYPES | USE_TYPES | {DISCOVER_TYPE} else "fight" if o.get("type") in FIGHT_TYPES else "craft" if o.get("type") == CRAFT_TYPE else None
                if kind and kind not in self.kinds: continue
                text = gamequests.objective_text(oid)
                if o.get("map") and int(o["map"]) in tutorial.MAPS and self.s.map_id not in tutorial.MAPS: continue      # the tutorial maps are closed behind us: the server finishes those quests
                if o.get("type") == 0 and re.match(r"\s*r[ée]gler le probl[èe]me", text or "", re.I): continue      # umbrella objective: done by the sub-quests it started
                if o.get("map") and here and not any_world and self._world(int(o["map"])) not in (None, here): continue      # another zone: not our business now
                if o.get("type") in TALK_TYPES | USE_TYPES:
                    if self.attempts[oid] >= (FREE_ATTEMPTS if o.get("type") in USE_TYPES else MAX_ATTEMPTS):
                        block(qid, oid, f"objectif « {text} » : échec après {self.attempts[oid]} essais")
                    elif o.get("map"):
                        tgt = int(o["map"])
                        if self.s.map_id != tgt and route(self.s.map_id, tgt) is None and not self._can_discover(tgt) and not self._portal_to(tgt):
                            block(qid, oid, f"objectif « {text} » : aucune route connue (porte non apprise ?)")
                        else: cands.append((0 if self.s.map_id == tgt else reach.get(tgt, 30), qid, oid))
                    elif o.get("type") == 0 and self.s.item_for_objective(oid) is not None: cands.append((0, qid, oid))      # a quest item in the bag completes it
                    elif o.get("type") == 0:
                        maps = self._known_location_maps(oid, text, reach, qid)
                        if maps: cands.append((reach[maps[0]], qid, oid))
                        else: block(qid, oid, f"objectif « {text} » : sous-zone ou carte introuvable")
                    else: block(qid, oid, f"objectif « {text} » : carte inconnue")
                elif o.get("type") == DISCOVER_TYPE:
                    tgt = self._discover_target(oid, qid)
                    if tgt is None: block(qid, oid, f"objectif « {text} » : carte inconnue (dis-moi où elle est)")
                    elif self.attempts[oid] >= MAX_ATTEMPTS: block(qid, oid, f"objectif « {text} » : échec après {self.attempts[oid]} essais")
                    elif self.s.map_id != tgt and route(self.s.map_id, tgt) is None and not self._can_discover(tgt) and not self._portal_to(tgt): block(qid, oid, f"objectif « {text} » : aucune route vers la carte {tgt}")
                    else: cands.append((0 if self.s.map_id == tgt else reach.get(tgt, 30), qid, oid))
                elif o.get("type") == CRAFT_TYPE:
                    if not o.get("p") or not gamejobs.recipe(int(o["p"][0])): block(qid, oid, f"objectif « {text} » : recette inconnue")
                    elif self.attempts[oid] >= MAX_ATTEMPTS: block(qid, oid, f"objectif « {text} » : échec après {self.attempts[oid]} essais")
                    else: cands.append((reach.get(int(o["map"]), 30) if o.get("map") else 30, qid, oid))
                elif o.get("type") in FIGHT_TYPES and self.attempts[oid] < FIGHT_ATTEMPTS and (
                        int(o["p"][0]) in {m for ms in self.s.groups.values() for m in ms} or gamedata.monster_maps(int(o["p"][0])) or o.get("map")):
                    tgt = int(o["map"]) if o.get("map") else 0
                    here_group = int(o["p"][0]) in {m for ms in self.s.groups.values() for m in ms}
                    if not here_group and not tgt and self._fight_world(o) not in (None, here):      # the monster lives in another world (Astrub's cats from Incarnam)
                        if any_world: cands.append((40, qid, oid))
                        continue
                    cands.append((0 if here_group else reach.get(tgt, 12) if tgt else 12, qid, oid))
                elif o.get("type") in FIGHT_TYPES: block(qid, oid, f"objectif « {text} » : aucun groupe de ce monstre sur la carte actuelle")
                else: block(qid, oid, f"objectif « {text} » : type non géré")
        if cands:
            _d, qid, oid = min(cands); return (qid, oid), None
        return None, ("aucun objectif géré — " + unsupported) if unsupported else "aucune quête en cours"

    def _fight_world(self, o):
        """World where the monsters of a fight objective live when none of them is in the current world (None = here, or unknown)."""
        here = self._world(self.s.map_id) if self.s.map_id else None
        worlds = {self._world(m) for m in gamedata.monster_maps(int(o["p"][0]))} - {None}
        if not worlds or here in worlds: return None
        return sorted(worlds)[0]

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

    def _guide_coords(self, qid, text):
        """Coordinates the scraped guide gives for an objective: the sentence of the quest's steps sharing the most words with the objective
        (NPC names first) and holding a [x,y]. -> {"x", "y"} or None."""
        guide = dpln.find((gamedata.quest_data("quests", qid) or {}).get("name")) if qid else None
        if not guide or not text: return None
        words = re.findall(r"[^\W\d_]{4,}", text); skip = {"aller", "voir", "parler", "vaincre", "chercher", "retourner", "donner", "examiner", "fouiller", "feuilleter", "dans", "avec", "pour", "les", "des"}
        toks = [(gamejobs.norm(w), 2 if w[0].isupper() else 1) for w in words if gamejobs.norm(w) not in skip]
        if not toks: return None
        best = None
        for step in guide.get("steps") or []:
            for sent in re.split(r"(?<=[.!?])\s+|\n", step):
                m = re.search(r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]", sent)
                if not m: continue
                n = gamejobs.norm(sent); score = sum(w for t, w in toks if re.search(r"(?<![a-z])" + re.escape(t), n))
                if score and (best is None or score > best[0]): best = (score, int(m[1]), int(m[2]))
        return {"x": best[1], "y": best[2]} if best else None

    def _known_location_maps(self, oid, text, reach=None, qid=None):
        """Maps for a mapless free-text objective: the exact place remembered in the quest book (coordinates) first, then the one the scraped guide gives,
        then the subarea named in the text."""
        hint = questbook.get_hint(oid)
        exact = self._hint_maps(hint) if hint and hint.get("x") is not None else []
        if not exact and (gc := self._guide_coords(qid, text)): exact = self._hint_maps(gc)
        return exact + [m for m in self._objective_location_maps(text, reach) if m not in exact]

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
                                             qd.get("level"), sd.get("name"), sd.get("desc"), dpln.walkthrough(quest))
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
                elif a["action"] == "attack": self._fight_for("xp"); self._act(f"Attaquer le groupe {a['group']}", lambda: self.s._do_attack(a["group"]))
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

    def _best_group(self, wanted, max_size=None):
        """Group of the current map covering the most wanted monsters (then the smallest, then the nearest), or None."""
        from fightbot import dist as cell_dist
        with self.s._lock: groups = {a: list(ms) for a, ms in self.s.groups.items()}; actors = dict(self.s.actors); me = self.s.my_cell
        best = None
        for a, ms in groups.items():
            score = len(set(ms) & wanted)
            if not score or len(ms) > (max_size or QUEST_MAX_GROUP): continue
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
        self._fight_for("boss", boss=True)
        try: self._act("Combat de quête", fight)
        except RuntimeError as e: self.say("warn", f"combat impossible : {e}"); return False
        return self._boss_played

    def _fight_objective(self, qid, oid):
        """Fight the best group on this map; if there is none, visit the nearest map where a wanted monster lives (up to MAX_SEARCH_MAPS)."""
        self.attempts[oid] += 1
        self._no_small_group_left = False
        if self._boss_fight(oid): return True
        visited = set(); travel_failures = 0
        for _ in range(MAX_SEARCH_MAPS + 1):
            if not self._alive(): return False
            wanted = self._wanted_monsters()
            if not wanted: self.say("info", "plus de monstre à vaincre"); return True
            names = ", ".join(sorted(gamedata.quest_data("monsters", m) or str(m) for m in wanted))
            self.goal = f"vaincre : {names}"
            group = self._best_group(wanted, MAX_FIGHT_GROUP) or (self._best_group(wanted) if self._no_small_group_left else None)      # groups the fight AI can take first; a bigger one (it killed the character: 5 pichons) only once every reachable map was searched
            if group is not None:
                with self.s._lock: ms = list(self.s.groups.get(group, []))
                self.say("info", f"combat contre {', '.join(gamedata.quest_data('monsters', m) or str(m) for m in ms)}")
                self._fight_for("quest")
                try: self._act(f"Attaquer le groupe {group}", lambda: self.s._do_attack(group, max_group=QUEST_MAX_GROUP))
                except RuntimeError as e: self.say("warn", f"combat impossible : {e}"); return self.attempts[oid] < FIGHT_ATTEMPTS
                time.sleep(REST_SECONDS)   # the character regains a little life before the next fight; also lets the quest updates arrive
                return True
            cur = self.s.map_id; visited.add(cur)
            dist = distances(cur); cands = ({m for w in wanted for m in gamedata.monster_maps(w)} | set(self._objective_maps())) - visited
            reachable = sorted((dist[m], m) for m in cands if m in dist)
            if not reachable and not self._no_small_group_left and self._best_group(wanted) is not None: self._no_small_group_left = True; continue      # nowhere else to look: accept the big group of this map
            if not reachable: self.say("warn", "aucune carte accessible où ces monstres apparaissent"); self.attempts[oid] = FIGHT_ATTEMPTS; return False      # no point asking again from here
            nxt = reachable[0][1]
            self.say("info", f"aucun groupe adapté ici : recherche sur la carte {nxt} ({reachable[0][0]} étape(s))")
            if not self._travel(nxt):
                visited.add(nxt); travel_failures += 1
                if travel_failures >= 2: self.say("error", "déplacements refusés à répétition : session à relancer, arrêt"); return False
            else: travel_failures = 0; time.sleep(2.0)   # let the map's actors arrive
        self.say("warn", "recherche de monstres abandonnée (trop de cartes visitées)"); return False

    # ---- one objective: travel, talk, answer ----
    # ---- "Découvrir la carte : X" (type 4): walk into the map ----
    def _discover_target(self, oid, qid=None):
        """Map an objective of type 4 asks to discover: its own map when it has one, else a map of that name, else the one the player told
        (knowledge/map_hints.json). -> map id or None."""
        o = gamedata.quest_data("objectives", oid) or {}
        if o.get("map"): return int(o["map"])
        txt = (o.get("txt") or "").strip()
        hint = knowledge.load("map_hints.json", {}).get(txt)
        if hint: return int(hint)
        guide = dpln.find((gamedata.quest_data("quests", qid) or {}).get("name")) if qid else None
        if guide and txt:         # "Le Tombeau de Percy Klop se trouve en [4,1]." in the scraped guide of the quest
            for step in guide.get("steps") or []:
                m = re.search(re.escape(txt) + r"[^.\[]*?se trouve en\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]", step, re.I)
                if m:
                    here = self._world(self.s.map_id); reach = distances(self.s.map_id) if self.s.map_id else {}
                    cands = [int(k) for k in gamedata.data().get("maps", {}) if (gamedata.map_info(int(k)) or {}).get("x") == int(m[1]) and (gamedata.map_info(int(k)) or {}).get("y") == int(m[2])
                             and self._world(int(k)) == here and (int(k) in reach or int(k) == self.s.map_id)]
                    if cands: return min(cands, key=lambda c: reach.get(c, 0))
        key = gamejobs.norm(txt)
        for mid in gamedata.data().get("maps", {}):
            info = gamedata.map_info(int(mid))
            if info and info["name"] and key and key in gamejobs.norm(info["name"]) and self._world(int(mid)) == self._world(self.s.map_id): return int(mid)
        return None

    def _discover_objective(self, qid, oid):
        tgt = self._discover_target(oid, qid); text = gamequests.objective_text(oid)
        self.attempts[oid] += 1; self.goal = f"{text} (carte {tgt})"
        self.say("info", f"objectif : {self.goal} — essai {self.attempts[oid]}/{MAX_ATTEMPTS}")
        done = lambda: not self.s.quests.get(qid, (None, {}))[1].get(oid, False)
        if self.s.map_id != tgt:
            if route(self.s.map_id, tgt) is None and not self._discover(tgt): return False
            if not self._travel(tgt): return False
        t0 = time.time()
        while self._alive() and time.time() - t0 < 6 and not done(): time.sleep(0.3)
        if not done():
            try: self.s.sync_quests()
            except Exception: pass
        if not self.s.quests.get(qid, (None, {}))[1].get(oid, False): self.say("info", "objectif accompli"); return True
        for _ in range(6):         # the place itself is behind a door of this map (a tomb, a building): test its doors until the objective is validated
            if not self._alive(): return False
            if self.s.map_id != tgt and not self._travel(tgt): return False
            self.say("info", f"carte {tgt} atteinte sans validation : je cherche une porte (tombeau, bâtiment…)")
            try: found = self.s.probe_doors()
            except RuntimeError as e: self.say("warn", f"test de porte impossible : {e}"); break
            if not found: break
            self._sleep(2.5)
            try: self.s.sync_quests()
            except Exception: pass
            if not self.s.quests.get(qid, (None, {}))[1].get(oid, False): self.say("info", "objectif accompli"); return True
        self.say("warn", "carte atteinte mais l'objectif n'est pas validé"); return self.attempts[oid] < MAX_ATTEMPTS

    # ---- changing world: a portal (NPCs), not a door ----
    def _explore_borders(self):
        """No route to an objective: try the borders of this map that no edge covers yet (the exit of an interior). Each map is tried once per run.
        -> True if the character left the map."""
        mid = self.s.map_id
        if mid in self._explored or len(self._explored) >= MAX_BORDER_EXPLORES: return False
        self._explored.add(mid)
        m = self.s.mapdata()
        if not m: return False
        known = {s for s, n in zip(SIDES, graph().get(mid, ())) if n} | {k.split(":")[1] for k in knowledge.load("border_targets.json", {}) if k.split(":")[0] == str(mid)}
        for side in SIDES:
            if not gamemaps.exit_cells(m, side): continue
            if side in known and (graph().get(mid, ()) or ())[SIDES.index(side)] in graph(): continue      # a normal border: the graph already follows it
            self.say("info", f"aucune route : j'essaie de sortir par le bord « {side} » de la carte {mid}")
            try: self._act(f"Explorer la sortie {side}", lambda: self.s._do_change_map(side))
            except RuntimeError as e: self.say("warn", f"sortie {side} impossible : {e}"); continue
            self.say("event", f"nouvelle carte : {self.s.map_id}"); self._sleep(2.0); return True
        return False

    def _retry_incarnam(self):
        """Before leaving the starting zone: Incarnam quests still open get one more round -- the objectives given up on earlier (a resource that was
        not back yet, a group that was not there) are tried again, except the ones the bot learned are dangerous or never validated. -> True if any."""
        import questreport
        self._incarnam_retried = True
        if self._incarnam_ids is None: self._incarnam_ids = set(questreport.area_quests("Incarnam"))
        with self.s._lock: quests = {q: dict(objs) for q, (_st, objs) in self.s.quests.items() if q in self._incarnam_ids}
        never = {int(k) for k in knowledge.load(self._kf(UNVALIDATED_FILE), {})}
        redo = [oid for objs in quests.values() for oid, active in objs.items() if active and self.attempts[oid] >= MAX_ATTEMPTS and oid not in self._danger and oid not in never]
        if not redo: return False
        for oid in redo: self.attempts[oid] = 0
        with knowledge._lock:
            d = knowledge.load(self._kf(GATHER_FAILED_FILE), {})
            for oid in redo: d.pop(str(oid), None)
            knowledge.save(self._kf(GATHER_FAILED_FILE), d)
        self.say("info", f"quêtes d'Incarnam encore ouvertes : je retente {len(redo)} objectif(s) abandonné(s) avant de partir")
        return True

    def _next_world(self):
        """Nothing left to do in this world: take a known portal to another world (once per run and portal) so that its quests can be started. -> True if the character arrived."""
        here = self._world(self.s.map_id) if self.s.map_id else None
        if not self._incarnam_retried and self._retry_incarnam(): return True
        for p in knowledge.load("portals.json", []):
            if p.get("from") != here or (here, p.get("to")) in self._worlds_tried: continue
            self._worlds_tried.add((here, p["to"])); self.say("info", f"plus rien à faire ici : je passe dans le monde « {p['to']} »")
            if self._change_world(p["to"]): return True
        return False

    def _portal_to(self, tgt):
        """Is `tgt` in another world that a known portal leads to?"""
        w, here = self._world(tgt), self._world(self.s.map_id)
        return bool(w and here and w != here and any(p.get('from') == here and p.get('to') == w for p in knowledge.load('portals.json', [])))

    def _change_world(self, world):
        """The objective is in another world (Incarnam -> Astrub): walk to the portal listed in knowledge/portals.json and talk to its NPCs in order
        (choosing the replies that name the portal). -> True when the character arrived in `world`."""
        here = self._world(self.s.map_id)
        for p in knowledge.load("portals.json", []):
            if p.get("from") != here or p.get("to") != world: continue
            tgt = int(p["map"])
            self.say("info", f"changement de monde : {here} → {world} par le portail de la carte {tgt}")
            if self.s.map_id != tgt and (route(self.s.map_id, tgt) is None or not self._travel(tgt)): self.say("warn", "portail hors de portée"); return False
            self._prefer_words = {gamejobs.norm(w) for w in p.get("prefer", [])}
            try:
                for npc in p.get("talk", []):
                    if not self._alive(): return False
                    ids = gamedata.npc_ids_named(npc)
                    actor = next((a for i in ids for a in [self._find_npc(i)] if a is not None), None)
                    if actor is None: self.say("warn", f"{npc} introuvable sur la carte {tgt}"); return False
                    try: self._act(f"Parler au PNJ {actor}", lambda: self.s._do_talk(actor))
                    except RuntimeError as e: self.say("warn", f"dialogue impossible : {e}"); return False
                    self._dialog(); self._sleep(2.5)
                    if self._world(self.s.map_id) == world: break
            finally: self._prefer_words = set()
            self._sleep(2.0)
            return self._world(self.s.map_id) == world
        self.say("warn", f"aucun portail connu de {here} vers {world}"); return False

    def _start_escort(self, qid, oid, text, target):
        """"Escorter Nowa et le livrer au chef Badufron": the NPC who must follow is named in the text and is not where the objective ends. Talk to him
        first (once per run), so that he follows, then the usual travel goes to the objective's map. -> True if a talk was done."""
        import questreport
        if oid in self._escorts: return False
        for npc, ids, maps in questreport.npcs_in_text(text):
            if target in maps: continue          # the one the escort ends with (the chief)
            reach = distances(self.s.map_id); ok = [m for m in maps if m == self.s.map_id or m in reach]
            if not ok: self.say("warn", f"{npc} (à escorter) est hors de portée"); continue
            tgt = min(ok, key=lambda m: reach.get(m, 0))
            self.say("info", f"escorte : je vais d'abord parler à {npc} (carte {tgt})")
            if self.s.map_id != tgt and not self._travel(tgt): continue
            actor = next((a for i in ids for a in [self._find_npc(i)] if a is not None), None)
            if actor is None: self.say("warn", f"{npc} introuvable sur la carte {tgt}"); continue
            try: self._act(f"Parler au PNJ {actor}", lambda: self.s._do_talk(actor))
            except RuntimeError as e: self.say("warn", f"dialogue impossible : {e}"); continue
            self._dialog(); self._escorts.add(oid); self._sleep(1.5); return True
        return False

    def _follow_guide_npcs(self, qid, oid):
        """An objective "talk to the occupants of ..." needs a chain of talks the objective does not spell out: do them in the order the scraped guide
        names the NPCs (a bartender, then a drinker, then someone upstairs), choosing the replies the guide quotes. -> True once the objective is done."""
        import questreport
        name = (gamedata.quest_data("quests", qid) or {}).get("name")
        seq = questreport.guide_npcs(name) if name else []
        if not seq: return False
        done = lambda: not self.s.quests.get(qid, (None, {}))[1].get(oid, False)
        self.say("info", f"guide de « {name} » : PNJ à voir dans l'ordre : " + ", ".join(n for n, _i, _m in seq))
        o = gamedata.quest_data("objectives", oid) or {}
        near = distances(int(o["map"])) if o.get("map") else None      # only the NPCs around the objective: the later steps of the guide (the militia, the councillor) are not for now
        for npc, ids, maps in seq:
            if not self._alive(): return False
            if near is not None: maps = [m for m in maps if near.get(m, 99) <= 3]
            reach = distances(self.s.map_id); ok = [m for m in maps if m == self.s.map_id or m in reach]
            if not ok: self.say("info", f"guide : {npc} trop loin de l'objectif ou hors de portée, je le laisse"); continue
            tgt = min(ok, key=lambda m: reach.get(m, 0))
            if self.s.map_id != tgt and not self._travel(tgt): continue
            actor = next((a for i in ids for a in [self._find_npc(i)] if a is not None), None)
            if actor is None: self.say("info", f"guide : {npc} absent de la carte {tgt}"); continue
            self.say("info", f"guide : je parle à {npc}")
            try: self._act(f"Parler au PNJ {actor}", lambda: self.s._do_talk(actor))
            except RuntimeError as e: self.say("warn", f"dialogue impossible : {e}"); continue
            self._dialog(); self._sleep(1.5)
            try: self.s.sync_quests()
            except Exception: pass
            if done(): self.say("info", "objectif accompli"); return True
        return done()

    def _talk_npcs(self, qid, oid, text):
        """Talk to the NPCs of the current map (the one named in the text first) and answer their dialogs until the objective is done.
        The reply index shifts with the attempt, so that the next tries walk other branches of the dialog."""
        with self.s._lock: npcs = dict(self.s.npcs)
        low = text.lower()
        named = [a for a, t in npcs.items() if (gamedata.quest_data("npcs", t) or "~").lower() in low]
        order = named or sorted(npcs)         # no name in the text (a nickname): talk to the NPCs of the map one after the other
        self.explore = max(0, self.attempts[oid] - 1)
        for actor in order[:6]:
            if not self._alive(): self.explore = 0; return False
            try: self._act(f"Parler au PNJ {actor}", lambda: self.s._do_talk(actor))
            except RuntimeError as e: self.say("warn", f"dialogue impossible : {e}"); continue
            self._focus_qid = qid
            try: self._dialog()
            finally: self._focus_qid = None
            time.sleep(1.5)
            try: self.s.sync_quests()
            except Exception: pass
            t0 = time.time()
            while self._alive() and time.time() - t0 < 4:
                if not self.s.quests.get(qid, (None, {}))[1].get(oid, False): self.explore = 0; self.say("info", "objectif accompli"); return True
                time.sleep(0.3)
        self.explore = 0
        return False

    def _item_named_in_text(self, text):
        """An inventory item whose name shares at least two significant words with the objective text (>= 4 letters, "Steff", "Taff", "légende"...). -> uid or None."""
        skip = {"feuilleter", "utiliser", "lire", "dans", "avec", "pour", "illustrant", "examiner", "consulter"}
        want = {w for w in gamejobs.norm(text).split() if len(w) >= 4 and w not in skip}
        best = None
        with self.s._lock: inv = {u: v[0] for u, v in self.s.inv.items()}
        for uid, gid in inv.items():
            have = {w for w in gamejobs.norm(gamejobs.item_name(gid) or "").split() if len(w) >= 4}
            n = len(want & have)
            if n >= 2 and (best is None or n > best[0]): best = (n, uid)
        return best[1] if best else None

    def _fetch_quest_item(self, qid, oid):
        """The guide says the quest item of an objective is bought ("achetez lui une limonade ... Vous obtenez alors une Limonade d'Incarnam dans votre
        inventaire de quête"): buy it from the NPC it names. -> the item's uid in the bag, or None."""
        uid = self.s.item_for_objective(oid)
        if uid is not None: return uid
        uid = self._item_named_in_text(gamequests.objective_text(oid))      # no effect links the item to the objective: the objective names it ("Feuilleter le livre ... Steff Taff")
        if uid is not None: return uid
        g = dpln.find((gamedata.quest_data("quests", qid) or {}).get("name")); name = None
        for step in (g or {}).get("steps") or []:
            m = re.search(r"obtenez (?:alors )?(?:une?|du|de la|des|d')\s*([A-ZÉÈ][\w' ]+?) dans votre inventaire de qu", step)
            if m: name = m.group(1).strip(); break
        if not name: return None
        want = gamejobs.norm(name)
        iid = next((int(i) for i, it in gamedata.data().get("items", {}).items() if gamejobs.norm((it if isinstance(it, dict) else {}).get("name") or "") == want), None)
        if iid is None: return None
        if self.s.have(iid) <= 0:
            self.say("info", f"objectif : l'objet de quête « {name} » s'achète : je vais le chercher")
            if not self._buy_item(iid, 1, qid): return None
        return self.s.item_for_objective(oid)

    def _use_objective(self, qid, oid, target):
        text = gamequests.objective_text(oid); self.goal = f"{text} (carte {target})"
        self.attempts[oid] += 1
        self.say("info", f"objectif : {self.goal} — essai {self.attempts[oid]}/{FREE_ATTEMPTS}")
        if re.match(r"\s*(escort|accompagn|guider|conduire)", text.lower()) and self._start_escort(qid, oid, text, target): pass
        item_uid = self._fetch_quest_item(qid, oid) if self._alive() else None
        if self.s.map_id != target and route(self.s.map_id, target) is None and not self._discover(target): return False
        if not self._travel(target): return False
        time.sleep(2.0)
        done = lambda: not self.s.quests.get(qid, (None, {}))[1].get(oid, False)
        if item_uid is not None:      # the quest item of the objective, used on the spot (the element it is meant for is not in the map data)
            self.say("info", "j'utilise l'objet de quête de l'objectif")
            try: self._act("Utiliser l'objet de quête", lambda: self.s._do_use_item(item_uid))
            except RuntimeError as e: self.say("warn", f"objet de quête inutilisable : {e}")
            t0 = time.time()
            while self._alive() and time.time() - t0 < 4 and not done(): time.sleep(0.4)
            if done(): self.say("info", "objectif accompli"); return True
        if re.match(r"\s*(parler|discuter|retourner|aller voir|escort|accompagn|guider|conduire)", text.lower()):       # free-text "talk to ..." objective (an escort ends with a talk to the chief): the NPC is named in the text
            if self._talk_npcs(qid, oid, text) or self._follow_guide_npcs(qid, oid): return True
            self.say("warn", "objectif non validé après avoir parlé aux PNJ de la carte"); return self.attempts[oid] < FREE_ATTEMPTS
        words = [w for w in re.findall(r"[a-zéèêàç]{4,}", text.lower()) if w not in ("lire", "utiliser", "contant", "légende", "dans", "avec")]
        done = lambda: not self.s.quests.get(qid, (None, {}))[1].get(oid, False)
        try: ok = self.s.use_recorded(oid, done) or self.s.use_for_objective(words, done)
        except RuntimeError as e: self.say("warn", f"utilisation impossible : {e}"); return self.attempts[oid] < FREE_ATTEMPTS
        if not ok:
            try: self.s.sync_quests()
            except Exception: pass
            ok = done()
        if not ok and self._alive():       # no named element did it: the way is a door of this map (the cellar of a tavern): doors before NPCs
            try: found = self.s.probe_doors()
            except RuntimeError as e: self.say("warn", f"test de porte impossible : {e}"); found = False
            if found: self.say("event", f"porte trouvée : carte {self.s.map_id}"); self._sleep(2.0); return self.attempts[oid] < FREE_ATTEMPTS
        if not ok and self.s.npcs and self._alive():       # still nothing: an NPC of the map may open the way (the gravedigger lets you into the crypt...)
            self.say("info", "aucun élément ne valide l'objectif : j'essaie de parler aux PNJ de la carte")
            ok = self._talk_npcs(qid, oid, text) or self._follow_guide_npcs(qid, oid)
        if ok: self.say("info", "objectif accompli"); return True
        self.say("warn", "objectif non validé après l'essai des éléments et des PNJ de la carte")
        o = gamedata.quest_data("objectives", oid) or {}; elem = int((o.get("p") or [0])[0] or 0)
        if elem and elem not in gamemaps.element_positions(target):      # the element the objective names is not on its map: more tries would repeat the same uses
            if self.attempts[oid] < MAX_ATTEMPTS: return True
            with knowledge._lock: d = knowledge.load(self._kf(UNVALIDATED_FILE), {}); d[str(oid)] = text; knowledge.save(self._kf(UNVALIDATED_FILE), d)       # later runs leave it alone at once
            self.attempts[oid] = 99; return False
        return self.attempts[oid] < FREE_ATTEMPTS

    def _talk_hinted_npc(self, oid, maps, npc_name, done):
        """A mapless "Chercher X" objective whose hint names an NPC: go to the hinted map and talk to that NPC (several times: a first talk may only start a chain).
        -> True once the objective is done."""
        ids = [int(i) for i in gamedata.npc_ids_named(npc_name)]
        if not ids: return False
        for mid in maps[:3]:
            if not self._alive(): return False
            if self.s.map_id != mid and not self._travel(mid): continue
            time.sleep(2.0)
            for _ in range(3):
                actor = next((a for i in ids for a in [self._find_npc(i)] if a is not None), None)
                if actor is None: break
                self.say("info", f"je parle à {npc_name} (indice de l'objectif)")
                try: self._act(f"Parler au PNJ {actor}", lambda: self.s._do_talk(actor))
                except RuntimeError as e: self.say("warn", f"dialogue impossible : {e}"); break
                self._dialog(); self._sleep(1.5)
                try: self.s.sync_quests()
                except Exception: pass
                if done(): self.say("info", "objectif accompli"); return True
        return done()

    def _use_location_objective(self, qid, oid):
        uid = self.s.item_for_objective(oid) or self._item_named_in_text(gamequests.objective_text(oid))       # "read the recipe": a quest item in the bag completes it (iql), no map to search; the item may only be named by the objective ("Steff Taff")
        if uid is not None:
            self.attempts[oid] += 1; self.goal = gamequests.objective_text(oid)
            self.say("info", f"objectif : {self.goal} — j'utilise l'objet de quête de mon inventaire")
            self._act("Utiliser l'objet de quête", lambda: self.s._do_use_item(uid))
            for _ in range(4):      # a book opens as a dialog (pages, then "go and complain to ..."): answer it with the guide of this quest
                if not self.s.dialog or not self._alive(): break
                self.say("info", "l'objet de quête a ouvert un dialogue : je le parcours")
                self._focus_qid = qid
                try: self._dialog()
                finally: self._focus_qid = None
                self._sleep(1.0)
            t0 = time.time()
            while self._alive() and time.time() - t0 < 4:
                if not self.s.quests.get(qid, (None, {}))[1].get(oid, False): self.say("info", "objectif accompli"); return True
                time.sleep(0.4)
            return self.attempts[oid] < 2      # the item was used and nothing was validated (a book with pages the bot cannot turn yet): not worth repeating
        text = gamequests.objective_text(oid); maps = self._known_location_maps(oid, text, qid=qid)
        if not maps:
            self.say("warn", f"objectif « {text} » : aucune carte accessible dans la sous-zone indiquée"); return False
        self.attempts[oid] += 1; self.goal = text
        self.say("info", f"objectif : {self.goal} — recherche dans {len(maps)} carte(s) accessible(s)")
        words = [w for w in re.findall(r"[a-zéèêàç]{4,}", text.lower()) if w not in ("lire", "utiliser", "contant", "légende", "dans", "avec")]
        if "vestige" in words: words = ["vestige"]
        done = lambda: not self.s.quests.get(qid, (None, {}))[1].get(oid, False)
        hint = questbook.get_hint(oid) or {}
        if hint.get("npc") and re.match(r"\s*(chercher|trouver)", text.lower()) and self._talk_hinted_npc(oid, maps, hint["npc"], done): return True
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
                unnamed = [e for e in elements.values() if e.get("interactive") is None and e.get("skill")]   # steles / vestiges have no model name
                if not unnamed and not any(any(w in name.casefold() for w in words) for name in names): continue
                self.say("info", f"élément correspondant trouvé sur la carte {mid}" + (f" ({len(unnamed)} sans nom)" if unnamed else ""))
                t0 = time.time()
                try: ok = self.s.use_for_objective(words, done, strict=False)
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
        if o.get("type") == CRAFT_TYPE: return self._craft_objective(qid, oid)
        if o.get("type") == DISCOVER_TYPE: return self._discover_objective(qid, oid)
        if o.get("type") in USE_TYPES:
            return self._use_objective(qid, oid, target) if target else self._use_location_objective(qid, oid)
        npc_id = int(o["p"][0])
        if o.get("type") == 3 and len(o["p"]) >= 3 and gamejobs.item_name(int(o["p"][1])):      # "Ramener à #1 : x#3 #2": have the item first
            item, qty = int(o["p"][1]), max(1, int(o["p"][2] or 1))
            if self.s.have(item) < qty:
                self.attempts[oid] += 1; self._acquiring_for = qid
                self.say("info", f"objectif : {gamequests.objective_text(oid)} — il me manque {qty - self.s.have(item)} {gamejobs.item_name(item)} : je vais le chercher")
                try: got = self._gather(item, qty)
                finally: self._acquiring_for = None
                if not got: return self.attempts[oid] < MAX_ATTEMPTS and self._alive()
        self.goal = f"{gamequests.objective_text(oid)} (carte {target})"
        self.attempts[oid] += 1
        self.say("info", f"objectif : {self.goal} — essai {self.attempts[oid]}/{MAX_ATTEMPTS}")
        if self.s.map_id != target and route(self.s.map_id, target) is None and not self._discover(target): return False
        if not self._travel(target): return False
        actor = self._find_npc(npc_id)
        if actor is None:
            self.say("warn", f"PNJ {gamedata.quest_data('npcs', npc_id) or npc_id} introuvable sur la carte"); return self.attempts[oid] < MAX_ATTEMPTS
        try: self._act(f"Parler au PNJ {actor}", lambda: self.s._do_talk(actor))
        except RuntimeError as e:
            self.say("warn", f"dialogue impossible : {e}")
            if "(jnz)" in str(e) and self._alive():       # the server refuses our moves: the position we believe is stale, a new connection tells the real one
                self.say("warn", "déplacements refusés : reconnexion pour resynchroniser la position")
                if self.s.reconnect(): self.attempts[oid] -= 1
            return self.attempts[oid] < MAX_ATTEMPTS
        self._focus_qid = qid
        try: self._dialog()
        finally: self._focus_qid = None
        time.sleep(1.5)
        try: self.s.sync_quests()   # a finished quest sends no message of its own: ask for each quest, drop the silent ones
        except Exception: pass
        t0 = time.time()   # the server confirms with ibg/icj shortly after the dialog ends
        while self._alive() and time.time() - t0 < 4:
            if not self.s.quests.get(qid, (None, {}))[1].get(oid, False): self.say("info", "objectif accompli"); return True
            time.sleep(0.3)
        self.say("warn", "objectif non validé après le dialogue"); return self.attempts[oid] < MAX_ATTEMPTS

    # ---- jobs: gather the ingredients, craft ----
    def _sleep(self, seconds):
        end = time.time() + seconds
        while self._alive() and time.time() < end: time.sleep(0.25)

    def _note_resources(self, models):
        """Remember the maps where an element of `models` stands (shared knowledge: the next search starts there)."""
        with self.s._lock: here = {v.get("type") for v in self.s.elements.values()}
        found = [m for m in models if m in here]
        if not found or not self.s.map_id: return
        with knowledge._lock:
            d = knowledge.load(RESOURCE_MAPS, {}); changed = False
            for m in found:
                lst = d.setdefault(str(m), [])
                if self.s.map_id not in lst: lst.append(self.s.map_id); changed = True
            if changed: knowledge.save(RESOURCE_MAPS, d)

    def _resources_here(self, models):
        """Harvestable elements of `models` on the current map (known cell, not being harvested / growing back), nearest first."""
        import gamemaps
        now = time.time()
        with self.s._lock: els = {k: dict(v) for k, v in self.s.elements.items()}; me = self.s.my_cell
        cells = gamemaps.element_cells(self.s.map_id) if self.s.map_id else {}
        ok = [e for e, v in els.items() if v.get("type") in models and v.get("skill") and self.s.unavailable.get(e, 0) < now]
        pos = gamemaps.element_positions(self.s.map_id) if self.s.map_id else {}
        learned = knowledge.load("element_cells.json", {}).get(str(self.s.map_id), {})
        def d(e):
            c = els[e].get("cell") if els[e].get("cell") is not None else learned.get(str(e), cells.get(e))
            if c is None: c = gamemaps.estimate_cell(pos[e]) if e in pos else None
            if me is None or c is None: return 0
            (x, y), (a, b) = gamemaps.to_xy(me), gamemaps.to_xy(c); return max(abs(x - a), abs(y - b))
        return sorted(ok, key=d)

    def _easiest_group(self):
        """Smallest monster group of the current map that the fight AI can take (MAX_GROUP), nearest first."""
        from fightbot import dist as cell_dist
        with self.s._lock: groups = {a: list(ms) for a, ms in self.s.groups.items()}; actors = dict(self.s.actors); me = self.s.my_cell
        ok = [a for a, ms in groups.items() if 0 < len(ms) <= QUEST_MAX_GROUP]
        if not ok: return None
        return min(ok, key=lambda a: (len(groups[a]), cell_dist(me, actors[a]) if me is not None and a in actors else 99))

    def _drop_monsters(self, item_id, qid):
        """Monsters that drop `item_id`, read in the scraped guide of the quest: the sentence that names the item and talks about a drop, and the
        monsters (questdata names, plural allowed) it names. -> set of monster ids."""
        g = dpln.find((gamedata.quest_data("quests", qid) or {}).get("name")) if qid else None
        item = gamejobs.norm(gamejobs.item_name(item_id))
        if not g or not item: return set()
        gamedata.quest_data("monsters", 0)
        names = {}
        for mid, nm in (gamedata._quest or {}).get("monsters", {}).items():
            n = gamejobs.norm(nm)
            if len(n) >= 4: names.setdefault(n, set()).add(int(mid))
        out = set(); sentences = []
        for step in g.get("steps") or []:
            sn = gamejobs.norm(step)
            if not gamejobs.mentions(item, sn) or not re.search(r"drop|loot|butin|obtient|r[ée]cup", sn): continue
            sentences.append(sn)
            hit = [n for n in names if re.search(r"(?<![a-z0-9])" + re.escape(n) + r"s?(?![a-z0-9])", sn)]
            for n in hit:
                if not any(n != m and n in m for m in hit): out |= names[n]      # "tofu" inside "tofu chimerique" is the same monster
                if len(n) >= 6 and not any(n != m and n in m for m in hit): out |= {i for nm, ids in names.items() if nm[:5] == n[:5] for i in ids}      # Incarnam's variants (Bouftou Nuageux, Boufton Pâlichon, Bouftor Éthéré)
        if not out and sentences:       # only a zone is named ("dans la Forêt d'Incarnam"): the monsters of that zone
            D = gamedata.data(); area = (gamedata.map_info(self.s.map_id) or {}).get("area") if getattr(self, "s", None) and self.s.map_id else "Incarnam"
            area_ids = {k for k, v in D.get("areas", {}).items() if v[0] == area}
            for sid, sub in D.get("subareas", {}).items():
                n = gamejobs.norm(sub[0])
                if str(sub[1]) in area_ids and len(n) >= 4 and any(re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", st) for st in sentences):
                    out |= set(D.get("subarea_monsters", {}).get(sid, []))
        return out

    def _buy_item(self, item_id, need, qid):
        """An item sold by an NPC (the guide: "Grobid vous vendra du Lailait pour 4 kamas"): go to the seller and talk to him, choosing in his dialog
        the replies that name the item. -> True when the inventory holds `need`."""
        import questreport
        name = gamejobs.item_name(item_id)
        sel = questreport.seller(name, (gamedata.quest_data("quests", qid) or {}).get("name")) if qid else None
        if not sel: return False
        npc, ids, maps = sel
        reach = distances(self.s.map_id); maps = [m for m in maps if m == self.s.map_id or route(self.s.map_id, m) is not None]
        if not maps: self.say("warn", f"{npc} (qui vend {name}) est hors de portée"); return False
        tgt = min(maps, key=lambda m: reach.get(m, 0))
        self.say("info", f"achat : {name} ({self.s.have(item_id)}/{need}) chez {npc} (carte {tgt})"); self.goal = f"acheter {need} {name}"
        self._prefer_words = {gamejobs.norm(name)} | ({gamejobs.norm(name).split()[0]} if len(gamejobs.norm(name).split()[0]) >= 5 else set())
        try:
            for _ in range(need - self.s.have(item_id) + 2):
                if not self._alive() or self.s.have(item_id) >= need: break
                if self.s.map_id != tgt and not self._travel(tgt): return False
                actor = next((a for i in ids for a in [self._find_npc(i)] if a is not None), None)
                if actor is None: self.say("warn", f"{npc} introuvable sur la carte {tgt}"); return False
                try: self._act(f"Parler au PNJ {actor}", lambda: self.s._do_talk(actor))
                except RuntimeError as e: self.say("warn", f"dialogue impossible : {e}"); return False
                self._dialog(); self._sleep(1.5)
        finally: self._prefer_words = set()
        return self.s.have(item_id) >= need

    def _hunt(self, item_id, need, wanted=None):
        """Meat comes from monsters: with the hunter's knife equipped, every monster killed may leave `item_id`. Fight the easiest group of this map,
        then of the nearest maps of the zone, until the inventory holds `need`. `wanted`: monster ids that drop the item (then no knife is needed and
        their groups, on the maps where they live, are the targets)."""
        name = gamejobs.item_name(item_id) or str(item_id)
        knives = gamejobs.hunting_knives()
        if wanted is None and not (knives & self.s.equipped):
            owned = [g for g in knives if any(v[0] == g for v in self.s.inv.values())]
            if not owned:
                self.say("warn", f"« {name} » est un butin de chasseur : il faut un couteau de chasse dans l'inventaire"); return False
            self.say("info", f"j'équipe « {gamejobs.item_name(owned[0])} » pour chasser")
            try: self._act("Équiper le couteau de chasse", lambda: self.s._do_equip(owned[0]))
            except RuntimeError as e: self.say("warn", f"équipement impossible : {e}"); return False
        self.say("info", f"chasse : {name} ({self.s.have(item_id)}/{need}), couteau équipé"); self.goal = f"chasser : {need} {name}"
        visited = set(); fights = maps = 0
        while self._alive() and self.s.have(item_id) < need and fights < MAX_HUNT_FIGHTS:
            self._refresh_connection()
            group = self._best_group(set(wanted)) if wanted else self._easiest_group()
            if group is not None:
                with self.s._lock: ms = list(self.s.groups.get(group, []))
                self.say("info", f"combat contre {', '.join(gamedata.quest_data('monsters', m) or str(m) for m in ms)}")
                self._fight_for("hunt")
                try: self._act(f"Chasse : groupe {group}", lambda: self.s._do_attack(group, max_group=QUEST_MAX_GROUP))
                except RuntimeError as e: self.say("warn", f"combat impossible : {e}"); visited.add(self.s.map_id)
                fights += 1; time.sleep(REST_SECONDS + 3); continue
            cur = self.s.map_id; visited.add(cur); dist = distances(cur); world = self._world(cur)
            cands = [m for m in dist if m not in visited and self._world(m) == world and dist[m] <= 6]
            if wanted: cands = [m for w in wanted for m in gamedata.monster_maps(w) if m in dist and m not in visited and self._world(m) == world] or cands      # where those monsters live, in this world (else the nearby maps: the zone's own monster list can lack them; never another world)
            maps += 1
            if not cands or maps > MAX_GATHER_MAPS: self.say("warn", f"plus de carte à visiter pour trouver un groupe à tuer ({name})"); return False
            nxt = min(cands, key=lambda m: dist[m])
            self.say("info", f"aucun groupe facile ici : je vais sur la carte {nxt}")
            if not self._travel(nxt): visited.add(nxt); continue
            self._sleep(2.0)
        return self.s.have(item_id) >= need

    def _enter_hint(self, item_id, tried):
        """The resource grows behind a door nobody has used yet (a mine): go to the maps the hints name (knowledge/resource_hints.json) and test their
        doors until one leads somewhere. -> True if a passage was found (the caller then looks for the resource where it arrived)."""
        h = knowledge.load("resource_hints.json", {}).get(str(item_id)) or {}
        left = [m for m in h.get("maps", []) if m not in tried]
        if not left: return False
        dist = distances(self.s.map_id)
        left = sorted(left, key=lambda m: dist.get(m, 99)); mid = left[0]; tried.add(mid)
        if mid not in dist and self.s.map_id != mid: self.say("warn", f"carte {mid} hors de portée (route inconnue)"); return False
        info = gamedata.map_info(mid)
        self.say("info", f"indice : {h.get('why', 'ressource derrière une porte')} : je vais sur la carte {mid}" + (f" ({info['subarea']} [{info['x']}, {info['y']}])" if info else ""))
        if self.s.map_id != mid and not self._travel(mid): return False
        self._sleep(2.0)
        from_map = self.s.map_id
        for _ in range(8):
            if not self._alive(): return False
            try: found = self.s.probe_doors()
            except RuntimeError as e: self.say("warn", f"test de porte impossible : {e}"); return False
            if found:
                self.say("event", f"porte trouvée : nouvelle carte {self.s.map_id}"); self._sleep(2.0); return self.s.map_id != from_map
            break
        self.say("warn", f"aucune porte utile sur la carte {mid}")
        return False

    def _gather(self, item_id, need, near=None):
        """Harvest `item_id` until the inventory holds `need`: the resources of this map first, then the nearest maps where one was seen (or any
        reachable map of the zone). `near`: a map (the workshop's) whose zone is searched first: the wood for the Forêt workshop grows in the Forêt.
        The job's experience comes with every harvest. -> True when enough."""
        name = gamejobs.item_name(item_id) or str(item_id); models = gamejobs.resource_skills(item_id)      # element types (= skill ids) that give this item
        if not models:
            qid = getattr(self, '_acquiring_for', None); wanted = self._drop_monsters(item_id, qid)
            if not wanted and self._buy_item(item_id, need, qid): return True      # sold by an NPC the guide names
            return self._hunt(item_id, need, wanted or None)      # a monster drop
        self.say("info", f"récolte : {name} ({self.s.have(item_id)}/{need})"); self.goal = f"récolter {need} {name}"
        visited = set(); fails = rounds = maps = doors_tried = 0; hinted = set(); silent = set(); unreachable = set(); harvested = 0      # unreachable: spots with no walkable cell next to them (a wrong cell estimate), forgotten for this search
        while self._alive() and self.s.have(item_id) < need:
            self._refresh_connection()
            self._note_resources(models)
            els = [el for el in self._resources_here(models) if (self.s.map_id, el) not in unreachable]
            if els:
                for el in els:
                    if not self._alive() or self.s.have(item_id) >= need: break
                    try: self._act(f"Récolter {name} (élément {el})", lambda: self.s._do_harvest(el)); fails = 0; silent.clear(); harvested += 1
                    except RuntimeError as e:
                        self.say("warn", f"récolte impossible : {e}")
                        if "aucune case accessible" in str(e): unreachable.add((self.s.map_id, el)); continue      # not the harvest's fault: look elsewhere
                        fails += 1
                        if "ne répond depuis" in str(e): silent.add(el)
                        if len(silent) >= MAX_SILENT_SPOTS and not harvested:      # several different spots that answer nothing: depletion would not look like that, the tool (a rod...) or the job level is missing
                            self.say("warn", f"{len(silent)} emplacements de {name} ne répondent pas : outil ou niveau de métier manquant ? je laisse la récolte"); return False
                        if fails >= 6: return False
                continue
            cur = self.s.map_id; visited.add(cur)
            only = [m for m in (knowledge.load("resource_hints.json", {}).get(str(item_id)) or {}).get("only", []) if m in distances(self.s.map_id) or m == self.s.map_id]      # maps the resource is known to grow on, and nowhere else
            if not only and self._enter_hint(item_id, hinted): continue       # behind a door told by the hints (a mine)
            dist = distances(cur); world = self._world(cur)
            seen = [m for mm in models for m in knowledge.load(RESOURCE_MAPS, {}).get(str(mm), [])]
            zones = {(gamedata.map_info(m) or {}).get("subarea") for m in seen if m in dist} - {None}      # where this resource has been seen: it grows in those zones (the Forêt for the ash)
            cands = [m for m in seen if m in dist and m not in visited] or \
                    [m for m in dist if m not in visited and (gamedata.map_info(m) or {}).get("subarea") in zones and dist[m] <= 30] or \
                    ([] if zones else [m for m in dist if m not in visited and self._world(m) == world and dist[m] <= 12])      # known zone exhausted: wait for the regrowth there, do not wander off
            if only: cands = [m for m in only if m not in visited]      # no wandering: the other maps of the zone are checked only when it is not this one
            if near:       # the workshop's zone first (then the nearest map)
                zone = (gamedata.map_info(near) or {}).get("subarea")
                cands.sort(key=lambda m: ((gamedata.map_info(m) or {}).get("subarea") != zone, dist[m]))
            if not cands and doors_tried < MAX_DOOR_PROBES:      # a closed area (a mine): its other maps are behind doors nobody has used yet
                doors_tried += 1
                self.say("info", f"aucune autre carte accessible : je teste les portes de la carte {cur}")
                try: found = self.s.probe_doors()
                except RuntimeError as e: self.say("warn", f"test de porte impossible : {e}"); found = False
                if found:
                    self.say("event", f"porte trouvée : carte {self.s.map_id}"); self._sleep(2.0); continue
            if not cands:
                rounds += 1
                if rounds > 6: self.say("warn", f"plus de carte à visiter pour trouver {name}"); return False
                self.say("info", f"aucun(e) {name} disponible : j'attends la repousse (le serveur la signale)"); visited.clear()
                for _ in range(20):      # up to a minute, ending as soon as one of them is back
                    if not self._alive() or self._resources_here(models): break
                    self._sleep(3)
                continue
            maps += 1
            if maps > MAX_GATHER_MAPS: self.say("warn", "trop de cartes visitées pour trouver la ressource"); return False
            nxt = cands[0] if near else min(cands, key=lambda m: dist[m]); info = gamedata.map_info(nxt)
            self.say("info", f"pas de {name} ici : je vais sur la carte {nxt}" + (f" ({info['subarea']} [{info['x']}, {info['y']}])" if info else ""))
            if not self._travel(nxt): visited.add(nxt); fails += 1; continue
            self._sleep(2.0)
        return self.s.have(item_id) >= need

    def _craft_objective(self, qid, oid):
        """"Fabriquer #2 #1 et fermer l'interface": gather what the recipe lacks, go to the workshop's map, craft, close the interface."""
        o = gamedata.quest_data("objectives", oid); item = int(o["p"][0]); qty = max(1, int(o["p"][1] or 1)); target = int(o.get("map") or 0)
        r = gamejobs.recipe(item); name = gamejobs.item_name(item) or str(item)
        self.attempts[oid] += 1; self.goal = f"fabriquer {qty} {name}"
        self.say("info", f"objectif : fabriquer {qty} {name} — recette : " + ", ".join(f"{q * qty} {gamejobs.item_name(i)}" for i, q in r["ingredients"]) +
                 f" ({gamedata.get('jobs', r['job'])}) — essai {self.attempts[oid]}/{MAX_ATTEMPTS}")
        for ing, q in r["ingredients"]:
            if self.s.have(ing) >= q * qty: continue
            if not gamejobs.resource_skills(ing) and target and self.s.map_id != target:       # a drop: hunt around the workshop (possibly in another world: portal)
                if route(self.s.map_id, target) is None and not self._discover(target): return False
                if not self._travel(target): return False
                self._sleep(2.0)
            self._acquiring_for = qid
            try: got = self._gather(ing, q * qty, near=target or None)
            finally: self._acquiring_for = None
            if not got:
                if gamejobs.resource_skills(ing) and self._alive():      # a harvested resource nobody found (a mine behind an unknown door): the same search again would fail the same way, the other quests go first
                    self.attempts[oid] = MAX_ATTEMPTS; self.say("warn", f"« {gamejobs.item_name(ing)} » introuvable : objectif laissé de côté pendant {GATHER_RETRY_HOURS} h")
                    with knowledge._lock: d = knowledge.load(self._kf(GATHER_FAILED_FILE), {}); d[str(oid)] = time.time(); knowledge.save(self._kf(GATHER_FAILED_FILE), d)
                return self.attempts[oid] < MAX_ATTEMPTS and self._alive()
        if not self._alive(): return False
        if target and self.s.map_id != target:
            if route(self.s.map_id, target) is None and not self._discover(target): return False
            if not self._travel(target): return False
        self._sleep(2.0)
        try: self._act(f"Fabriquer {qty} {name}", lambda: self.s._do_craft(item, qty))
        except RuntimeError as e: self.say("warn", f"fabrication impossible : {e}"); return self.attempts[oid] < MAX_ATTEMPTS and self._alive()
        try: self.s.sync_quests()
        except Exception: pass
        t0 = time.time()
        while self._alive() and time.time() - t0 < 6:
            if not self.s.quests.get(qid, (None, {}))[1].get(oid, False): self.say("info", "objectif accompli"); return True
            time.sleep(0.3)
        self.say("warn", "objectif non validé après la fabrication"); return self.attempts[oid] < MAX_ATTEMPTS

    # ---- quests that are not started yet: go and talk to the NPC the scraped guide names ----
    def _other_class_quest(self, name):
        """The guide's prerequisites say "Être un X" (a class quest): True when X is not this character's class."""
        g = dpln.find(name) or {}
        m = re.search(r"[ÊE]tre (?:un|une|le|la)\s+([^\n.,;/]+)", g.get("prereq") or "")
        if not m or not self.s.breed_id: return False
        mine = gamejobs.norm(gamedata.get("breeds", self.s.breed_id) or "")
        want = gamejobs.norm(m.group(1))
        return bool(mine) and bool(want) and mine != want and not want.startswith(("niveau", "membre", "dans", "a ", "en "))

    def _start_new_quests(self, incarnam_only=False):
        """Nothing left to do on the active quests: start one of the area's quests that was never begun (finished and active ones are skipped).
        The starter NPC comes from the scraped guide (questreport.starter), its map from the quests that send the player to it.
        -> True if a quest was started (the main loop then follows it)."""
        import questreport
        info = gamedata.map_info(self.s.map_id) if self.s.map_id else None
        if not info: return False
        with self.s._lock: done = set(self.s.finished_quests); active = set(self.s.quests)
        if not done or (self._new_quests >= MAX_NEW_QUESTS and not incarnam_only): return False      # finish what is started before opening more          # the finished list comes with the login: without it everything would look unstarted
        todo = dict(questreport.area_quests("Incarnam")) if info["area"] != "Incarnam" else {}      # the Incarnam quests left undone come first, wherever we are
        if not incarnam_only: todo.update(questreport.area_quests(info["area"]))
        for qid, q in sorted(todo.items(), key=lambda kv: (kv[0] not in questreport.area_quests("Incarnam"), kv[1]["level"] or 0, kv[0])):
            if qid in done or qid in active or qid in self._start_tried or str(qid) in knowledge.load("quests_ignored.json", {}): continue
            if (self.s.level or 0) <= knowledge.load(self._kf(START_FAILED_FILE), {}).get(str(qid), -99) + 4: continue      # the NPC did not start it (a condition is missing): not again before the character has grown a few levels
            if self.s.level and q["level"] and q["level"] > self.s.level + 4: continue      # too hard for now: the list is sorted by level, the next ones are even harder
            if self._other_class_quest(q["name"]): continue      # "Être un Huppermage": a class quest of another class never starts for this character
            self._start_tried.add(qid)
            offer = knowledge.load("quest_starters.json", {}).get(str(qid))
            if offer:         # the quest starts from an element (a job board), not from an NPC
                tgt = int(offer["map"]); self.say("info", f"nouvelle quête « {q['name']} » (niveau {q['level']}) : offre sur la carte {tgt}")
                if self.s.map_id != tgt and (route(self.s.map_id, tgt) is None or not self._travel(tgt)): self.say("warn", "carte de l'offre hors de portée"); continue
                self._sleep(2.0)
                try: self._act(f"Accepter la quête {q['name']}", lambda: self.s._do_accept_offer(qid, int(offer["element"]), offer.get("stand")))
                except RuntimeError as e: self.say("warn", f"offre impossible : {e}"); continue
                self._new_quests += 1; self.say("event", f"quête « {q['name']} » démarrée"); return True
            st = questreport.starter(q["name"])
            if not st: self.say("info", f"quête « {q['name']} » : PNJ de départ introuvable dans le guide, je la laisse"); continue
            npc, ids, maps = st
            reach = distances(self.s.map_id); maps = [m for m in maps if m == self.s.map_id or route(self.s.map_id, m) is not None or m in reach]
            if not maps: self.say("info", f"quête « {q['name']} » : {npc} est hors de portée (route inconnue)"); continue
            tgt = min(maps, key=lambda m: reach.get(m, 0))
            self.say("info", f"nouvelle quête « {q['name']} » (niveau {q['level']}) : je vais parler à {npc} (carte {tgt})")
            self.goal = f"démarrer « {q['name']} » auprès de {npc}"
            if self.s.map_id != tgt and not self._travel(tgt):
                if not self._alive(): self._start_tried.discard(qid); return False      # the connection dropped: not the quest's fault, and the next ones would fail the same way
                continue
            actor = self._find_npc(ids[0]) if len(ids) == 1 else next((a for i in ids for a in [self._find_npc(i)] if a is not None), None)
            def failed():
                with knowledge._lock:
                    d = knowledge.load(self._kf(START_FAILED_FILE), {}); d[str(qid)] = self.s.level or 0; knowledge.save(self._kf(START_FAILED_FILE), d)
            if actor is None: self.say("warn", f"{npc} introuvable sur la carte {tgt}"); failed(); continue
            try: self._act(f"Parler au PNJ {actor}", lambda: self.s._do_talk(actor))
            except RuntimeError as e:
                self.say("warn", f"dialogue impossible : {e}")
                if self._alive(): failed()
                continue
            self._dialog(); time.sleep(2.0)
            try: self.s.sync_quests()
            except Exception: pass
            with self.s._lock: started = qid in self.s.quests or qid in self.s.finished_quests
            if started: self._new_quests += 1; self.say("event", f"quête « {q['name']} » démarrée"); return True
            self.say("warn", f"« {q['name']} » n'a pas démarré après le dialogue avec {npc}")
            failed()
        return False

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
            if d <= 1: out.append((d, mi["subarea"] != gi["subarea"], steps, mid))      # the same zone first (the cellar of the Taverne is entered from inside the Taverne)
        return [mid for _d, _z, _s, mid in sorted(out)][:6]

    def _can_discover(self, goal):
        return self.discover_tries[goal] < 2 and bool(self._discover_maps(goal))

    def _discover(self, goal):
        """No known route to `goal`: visit the maps around it and probe their unknown doors until a route appears. A door that leads to a new map close
        to the goal (a house of the same square) is explored from the inside before going back: the goal may be a room of that building."""
        self.discover_tries[goal] += 1
        self.say("info", f"aucune route vers {goal} : découverte automatique des portes sur les cartes voisines")
        done = set()
        for _round in range(12):
            cands = [m for m in self._discover_maps(goal) if m not in done]
            if not cands or not self._alive(): break
            mid = cands[0]; done.add(mid)
            for _ in range(12):
                if not self._alive(): return False
                if self.s.map_id != mid:
                    if route(self.s.map_id, mid) is None: break
                    if not self._travel(mid): break
                if not self.s.probe_doors():
                    self.say("info", f"carte {mid} : plus de porte à essayer"); break
                if route(self.s.map_id, goal) is not None or self.s.map_id == goal: self.say("event", "route trouvée vers l'objectif"); return True
                self.say("info", "porte apprise mais ce n'est pas la bonne : j'explore ce nouveau bâtiment avant de poursuivre")
                done.discard(self.s.map_id)      # the new map is a candidate too (it is reachable now): it comes first when it is on the goal's square
                break
        return route(self.s.map_id, goal) is not None

    def _refresh_connection(self):
        """Connection renewal is disabled (the user does not want the bot to reconnect by itself every 5 min): kept so the callers stay valid."""
        return False

    def _travel(self, goal, ask_help=True):
        """Walk/use doors until map `goal` is reached. A step must change the map: if it does not, give up instead of looping."""
        prev = None; blocked_retries = 0; resyncs = 0; helps = 0
        for _ in range(MAX_TRAVEL_STEPS):
            cur = self.s.map_id
            if not self._alive(): return False
            if cur == goal: return True
            for _ in range(24):      # the actor list of a new map arrives a moment after jow: without our cell no exit can be planned (and the border would be wrongly noted as blocked)
                if self.s.my_cell is not None or not self._alive(): break
                time.sleep(0.25)
            if cur == prev: self.say("error", "le voyage n'avance plus (la carte n'a pas changé après l'étape), abandon"); return False
            r = route(cur, goal)
            if r is None:
                reason = f"aucune route de la carte {cur} à la carte {goal} (porte non apprise, zaap ou carte inconnue ?)"
                helps += 1
                if ask_help and helps <= 3 and self._ask_help(cur, reason): prev = None; continue
                self.say("error", reason); return False
            kind, arg = r[0]
            self.say("info", f"voyage vers {goal} : {len(r)} étape(s), prochaine : " + (arg if kind == "side" else f"case-déclencheur {arg['cell']}" if arg.get("kind") == "step" else f"porte (élément {arg['element']})"))
            key = (cur, arg if kind == "side" else arg.get("element") or arg.get("cell")); now = time.time()
            self._steps_taken.append((now, key))
            if sum(1 for t, k in self._steps_taken if k == key and now - t < 300) > 4:      # A->B->A->B... : every step changes the map, so the check below never fires
                self._steps_taken.clear(); helps += 1
                if ask_help and helps <= 3 and self._ask_help(cur, f"le voyage tourne en rond (même étape répétée depuis la carte {cur})"): prev = None; continue
                self.say("error", f"le voyage tourne en rond (même étape répétée depuis la carte {cur}), abandon"); return False
            prev = cur
            try:
                if kind == "side": self._act(f"Changer de carte ({arg})", lambda: self.s._do_change_map(arg))
                elif arg.get("kind") == "step": self._act(f"Marcher sur la case {arg['cell']}", lambda: self.s._do_step(arg["cell"], bool(arg.get("guess"))))
                else: self._act(f"Utiliser l'élément {arg['element']}", lambda: self.s._do_use(arg["element"]))
            except DesyncError as e:
                resyncs += 1
                if resyncs > 2: self.say("error", f"voyage interrompu : {e}"); return False
                self.say("warn", f"{e} — reconnexion ({resyncs}/2)")
                if not getattr(e, "reconnected", False) and not self.s.reconnect(): self.say("error", "reconnexion impossible"); return False
                prev = None; continue
            except RuntimeError as e:
                if kind == "side" and ("case de sortie" in str(e) or "carte voisine" in str(e)) and blocked_retries < 3:
                    block_edge(cur, arg); blocked_retries += 1; prev = None   # this border cannot be crossed on foot: remember it and plan another way
                    self.say("warn", f"sortie {arg} impossible depuis la carte {cur} (notée) : nouvel itinéraire"); continue
                if kind != "side" and arg.get("kind") == "step" and arg.get("guess") and self._alive():       # the exit guessed from the arrival cell was wrong: the way out is a door near it
                    self.say("warn", "sortie supposée sans effet : je cherche la porte près de la case d'arrivée")
                    try:
                        if self.s.probe_doors(): prev = None; continue
                    except RuntimeError as e2: self.say("warn", f"test de porte impossible : {e2}")
                if kind != "side" and arg.get("kind") == "step" and self.s.npcs and self._alive() and self._talk_any(cur):      # a closed room whose exit is not a border: its NPC may open it
                    prev = None; continue
                helps += 1
                if ask_help and helps <= 3 and self._alive() and self._ask_help(cur, f"étape impossible depuis la carte {cur} : {e}"): prev = None; continue
                self.say("error", f"voyage interrompu : {e}"); return False
        self.say("error", "voyage trop long, abandon"); return False

    def _ask_help(self, cur, reason):
        """Stuck on map `cur` (no known exit / the step failed): hand over to the user, who finds the cell or door by hand in the Map tab.
        What they do is learned as any passage (gamesession._learn_passage), so the next trip needs no help. -> True once the map changed."""
        self.say("warn", f"BLOQUÉ : {reason}. Prends la main dans l'onglet Map (clique la case de sortie, utilise la porte) : je l'apprends et je reprends ensuite ({HELP_WAIT // 60} min max)")
        self.help = {"map": cur, "reason": reason, "since": time.time()}
        notify.send("Dofus : autopilote bloque", f"{self.s.character or 'Personnage'} : {reason}. Trouve la case ou la porte a la main.")
        try:
            end = time.time() + HELP_WAIT
            while self._alive() and time.time() < end:
                if self.s.map_id != cur and self.s.map_id is not None:
                    time.sleep(2.0)      # let the passage be learned before planning again
                    self.say("event", "reprise de l'autopilote après ton intervention"); self._steps_taken.clear(); return True
                time.sleep(0.5)
            if self._alive(): self.say("error", "aucune intervention : abandon")
            return False
        finally: self.help = None

    def _talk_any(self, frm):
        """Talk to the NPCs of the current map one after the other (answering their dialogs) until the map changes. -> True if it did."""
        with self.s._lock: npcs = sorted(self.s.npcs)
        self.say("warn", f"sortie introuvable : je parle aux {len(npcs)} PNJ de la carte pour l'ouvrir")
        for actor in npcs[:4]:
            if not self._alive(): return False
            try: self._act(f"Parler au PNJ {actor}", lambda: self.s._do_talk(actor))
            except RuntimeError as e: self.say("warn", f"dialogue impossible : {e}"); continue
            self._dialog(); time.sleep(2.0)
            if self.s.map_id != frm: self.say("info", "la carte a changé après le dialogue"); return True
        return False

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

    def _reply_by_context(self, replies):
        """Pick the reply that talks about the active quests: its words (proper names count triple) are looked up in the quest names, the active
        objectives and the first steps of their scraped guides. None when no reply stands out. The quest of the objective being pursued is
        looked at alone first (a lailait asked by another quest must not win over the limonade of this one)."""
        with self.s._lock: quests = {q: dict(objs) for q, (_st, objs) in self.s.quests.items()}
        if self._focus_qid in quests:      # a talk objective of this quest: only its own guide counts (the words of the other quests only mislead: "boire" of Chacha Blues for Grobid's rat)
            return self._reply_in_quests(replies, {self._focus_qid: quests[self._focus_qid]}, 1, whole_guide=True)
        return self._reply_in_quests(replies, quests, 2)

    def _reply_in_quests(self, replies, quests, min_score, whole_guide=False):
        ctx = []
        for qid, objs in quests.items():
            name = (gamedata.quest_data("quests", qid) or {}).get("name") or ""
            ctx.append(name); ctx += [gamequests.objective_text(o) for o, active in objs.items() if active]
            g = dpln.find(name)
            if g: ctx += (g.get("steps") if whole_guide else (g.get("steps") or [])[:3]) or []
        stem = (lambda w: w[:5]) if whole_guide else (lambda w: w)      # focused quest: "annoncer" (reply) matches "annoncez" (guide)
        words = {stem(w) for w in gamejobs.norm(" ".join(ctx)).split()}
        import questreport
        quotes = set()
        for qid in quests: quotes |= questreport.quoted_replies((gamedata.quest_data("quests", qid) or {}).get("name"))
        def score(text):
            sc = 0
            for i, w in enumerate(re.findall(r"[^\W\d_]+", text)):
                n = gamejobs.norm(w)
                if len(n) >= 5 and stem(n) in words: sc += 3 if (i > 0 and w[0].isupper()) else 1
            if any(p and p in gamejobs.norm(text) for p in self._prefer_words): sc += 6      # the item being bought
            tn = gamejobs.squash(text)
            if any(len(q) >= 6 and (q in tn or tn in q) for q in quotes): sc += 10      # the guide quotes this very reply
            return sc
        scored = sorted(((score(x["text"]), x) for x in replies if not REFUSE.search(x["text"])), key=lambda t: -t[0])
        if scored and scored[0][0] >= min_score and (len(scored) == 1 or scored[0][0] > scored[1][0]): return scored[0][1]
        return None

    def _dialog_steps(self):
        for depth in range(30):
            d = self.s.dialog
            if not d or not self._alive(): return
            replies = d.get("replies") or []
            if not replies: time.sleep(1.5); self.s.close_dialog(); return
            d = self.s._dlg or {}
            r = questbook.suggest_reply(d.get("npc"), d.get("map"), d.get("path", []), replies, self.s._active_objectives(), REFUSE)
            src = "livre de quêtes" if r else "heuristique"
            if r is None and len(replies) > 1:
                r = self._reply_by_context(replies)
                if r: src = "contexte de la quête"
            if r is None and self.llm:
                quest = "; ".join(f"{o['quête']} : {o['objectif']}" for o in self.s.guide_state()["objectifs_actifs"][:4])
                npc = gamedata.quest_data("npcs", d.get("npc")) if d.get("npc") else None
                g, why = guide.choose_reply(quest, npc, (self.s.dialog or {}).get("text"), replies)
                if g: r, src = g, f"Groq : {why}"
            if r is None and self.explore:       # retry of a free-text talk objective: walk other branches of the dialog (reply index shifts with the attempt and the depth)
                pool = [x for x in replies if not REFUSE.search(x["text"])] or replies      # never "change your mind / stay": those only end the conversation
                r = pool[(self.explore + depth) % len(pool)]; src = f"exploration {self.explore}"
            r = r or pick_reply(replies, FIGHT_REPLY if self.want_fight else None)
            self.say("info", f"réponse ({src}) : « {r['text']} »")
            try: self._act(f"Répondre ({r['id']})", lambda: self.s._do_reply(r["id"]))
            except RuntimeError as e: self.say("warn", f"réponse impossible : {e}"); return
            time.sleep(0.4)

    # ---- farm: walk a route (farmroute.py) in a loop to farm one monster or one job resource, until a limit, full pods or a stop ----
    FARM_PODS = 0.95            # stop when the inventory is that full

    def start_farm(self, kind, ident, limit=0, max_group=4, zone=None, bank=False):
        """kind 'monster' (ident = monster id, limit = fights) or 'resource' (ident = item id, limit = units to harvest); limit 0 = until the pods are full.
        max_group: biggest monster group attacked. -> error string or None. Progress in self.farm (shown by the UI)."""
        import farmroute
        if self.on or self.s.status != "connected": return "autopilote déjà actif ou personnage non connecté"
        try: ident = int(ident); limit = max(0, int(limit or 0)); max_group = max(1, min(8, int(max_group or 4))); zone = None if zone in (None, "") else int(zone)
        except (TypeError, ValueError): return "paramètres invalides"
        kind = "monster" if kind == "monster" else "resource"
        if kind == "resource" and not gamejobs.resource_skills(ident): return "cet objet ne se récolte pas (c'est un drop de monstre)"
        route = farmroute.build(kind, ident, self.s.map_id, zone)
        if kind == "monster" and not route["maps"]: return "aucune carte accessible connue pour ce monstre"
        self.farm = {"kind": kind, "id": ident, "name": route["name"], "limit": limit, "max_group": max_group, "done": 0, "unit": "combats" if kind == "monster" else "unités",
                     "maps": route["maps"], "known": route["known"], "map": self.s.map_id, "state": "running", "msg": "", "t0": time.time(), "visited": 0, "bank": bool(bank), "trips": 0}
        self.quest_id = None; self.kinds = set(); self.llm = False; self.s.resume_auto = None
        self.on = True; self.goal = f"farm : {route['name']}"; self._stop = threading.Event(); self._deaths0 = getattr(self.s, "deaths", 0)
        self._thread = threading.Thread(target=self._farm_run, daemon=True); self._thread.start()
        return None

    def _farm_run(self):
        f = self.farm; self.say("info", f"farm démarré : {f['name']} ({'monstre' if f['kind'] == 'monster' else 'ressource'}), {len(f['maps'])} carte(s) sur le trajet")
        try:
            (self._farm_monster if f["kind"] == "monster" else self._farm_resource)(f)
            if f["state"] == "running": f["state"] = "stopped" if self._stop.is_set() else "done"
        except Exception as e:
            f["state"] = "error"; f["msg"] = f"{type(e).__name__}: {e}"; self.say("error", f"farm interrompu : {f['msg']}")
        finally:
            f["t1"] = time.time(); self.on = False; self.goal = None
            self.say("info", f"farm terminé ({f['state']}) : {f['done']} {f['unit']}" + (f" — {f['msg']}" if f["msg"] else ""))

    def _farm_pods_full(self):
        with self.s._lock: v = self.s._vitals() or {}
        return bool(v.get("pods") and v.get("pods_max") and v["pods"] >= self.FARM_PODS * v["pods_max"])

    def _farm_next_map(self, f, bad):
        """Next map of the route after the current one (loop), skipping the ones that could not be reached. None when none is left."""
        ids = [m["id"] for m in f["maps"] if m["id"] not in bad]
        if not ids: return None
        cur = self.s.map_id
        if cur in ids: return ids[(ids.index(cur) + 1) % len(ids)] if len(ids) > 1 else None
        d = distances(cur) if cur else {}
        return min(ids, key=lambda m: d.get(m, 10 ** 6))

    def _farm_stop_reason(self, f):
        if f["limit"] and f["done"] >= f["limit"]: return f"objectif atteint ({f['done']} {f['unit']})"
        if not f.get("bank") and self._farm_pods_full(): return "inventaire plein (pods)"
        if getattr(self.s, "deaths", 0) - self._deaths0 >= MAX_DEFEATS: return f"{MAX_DEFEATS} défaites : combat trop dur, arrêt"
        return None

    def _farm_bank(self, f):
        """Pods full: walk to the nearest known bank, empty the bag, come back to the route. -> False (farm stopped, reason in f['msg']) when it fails."""
        import farmroute
        back = self.s.map_id; bank = farmroute.nearest_bank(back)
        if not bank: f["msg"] = "inventaire plein et aucune banque connue (elle est mémorisée quand un banquier est rencontré)"; f["state"] = "stopped"; self.say("warn", f["msg"]); return False
        self.say("info", f"pods pleins : retour en banque ({bank['zone']} [{bank['x']}, {bank['y']}], {bank['steps']} pas)")
        if not self._travel(bank["id"]): f["msg"] = "banque injoignable"; f["state"] = "error"; return False
        try: self._act("Farm : dépôt en banque", self.s.bank_deposit)
        except RuntimeError as e: f["msg"] = str(e); f["state"] = "stopped"; self.say("warn", f"à la banque : {e}"); return False
        f["trips"] += 1
        if back != self.s.map_id and not self._travel(back): f["msg"] = "retour au trajet impossible"; f["state"] = "error"; return False
        return True

    def _farm_monster(self, f):
        wanted = {f["id"]}; bad = set(); fails = 0
        while self._alive():
            why = self._farm_stop_reason(f)
            if why: f["msg"] = why; self.say("info", why); f["state"] = "done" if "atteint" in why else "stopped"; return
            if f.get("bank") and self._farm_pods_full() and not self._farm_bank(f): return
            self._refresh_connection(); self._rest_hp(); f["map"] = self.s.map_id
            group = self._best_group(wanted, max_size=f["max_group"])
            if group is not None:
                with self.s._lock: ms = list(self.s.groups.get(group, []))
                self.say("info", f"combat contre {', '.join(gamedata.quest_data('monsters', m) or str(m) for m in ms)}")
                self._fight_for("hunt")
                try: self._act(f"Farm : groupe {group}", lambda: self.s._do_attack(group, max_group=f["max_group"])); f["done"] += 1; fails = 0
                except RuntimeError as e:
                    fails += 1; self.say("warn", f"combat impossible : {e}")
                    if fails >= 5: f["msg"] = "5 combats impossibles de suite"; f["state"] = "error"; return
                time.sleep(REST_SECONDS + 3); continue
            nxt = self._farm_next_map(f, bad)
            if nxt is None: f["msg"] = "aucune carte accessible sur le trajet"; f["state"] = "error"; return
            f["visited"] += 1; info = gamedata.map_info(nxt)
            self.say("info", f"plus de groupe ici : carte suivante du trajet {nxt}" + (f" ({info['subarea']} [{info['x']}, {info['y']}])" if info else ""))
            if not self._travel(nxt): bad.add(nxt); self.say("warn", f"carte {nxt} injoignable : retirée du trajet"); continue
            self._sleep(2.0)

    def _farm_resource(self, f):
        item = f["id"]; models = gamejobs.resource_skills(item); base = self.s.have(item); bad = set(); unreachable = set(); fails = 0; idle_rounds = 0
        if not f["maps"]:       # nothing known: the usual search explores the zone by itself and remembers where the resource grows
            self.say("info", "emplacements inconnus : exploration des cartes voisines (ils seront mémorisés)")
            need = base + (f["limit"] or 120)
            ok = self._gather(item, need); f["done"] = self.s.have(item) - base
            f["msg"] = "objectif atteint" if ok else "ressource introuvable"; f["state"] = "done" if ok else "error"; return
        while self._alive():
            f["done"] = max(0, self.s.have(item) - base); f["map"] = self.s.map_id
            why = self._farm_stop_reason(f)
            if why: f["msg"] = why; self.say("info", why); f["state"] = "done" if "atteint" in why else "stopped"; return
            if f.get("bank") and self._farm_pods_full() and not self._farm_bank(f): return
            self._refresh_connection(); self._note_resources(models)
            els = [el for el in self._resources_here(models) if (self.s.map_id, el) not in unreachable]
            if els:
                idle_rounds = 0
                for el in els:
                    if not self._alive() or self._farm_stop_reason(f) or (f.get("bank") and self._farm_pods_full()): break
                    try: self._act(f"Farm : récolter {f['name']} (élément {el})", lambda: self.s._do_harvest(el)); fails = 0
                    except RuntimeError as e:
                        self.say("warn", f"récolte impossible : {e}")
                        if "aucune case accessible" in str(e): unreachable.add((self.s.map_id, el)); continue
                        fails += 1
                        if fails >= 6: f["msg"] = "6 récoltes impossibles de suite (outil ou niveau de métier ?)"; f["state"] = "error"; return
                    f["done"] = max(0, self.s.have(item) - base)
                continue
            nxt = self._farm_next_map(f, bad)
            if nxt is None: f["msg"] = "aucune carte accessible sur le trajet"; f["state"] = "error"; return
            f["visited"] += 1; idle_rounds += 1
            if idle_rounds > 2 * len(f["maps"]):       # two whole laps without any resource: they are growing back
                self.say("info", "tout est récolté sur le trajet : j'attends la repousse"); self._sleep(45); idle_rounds = 0
            self.say("info", f"plus de {f['name']} ici : carte suivante du trajet {nxt}")
            if not self._travel(nxt): bad.add(nxt); self.say("warn", f"carte {nxt} injoignable : retirée du trajet"); continue
            self._sleep(2.0)
