"""Quest autopilot: picks the next supported quest objective, travels there (map graph), talks to the NPC and answers its dialog.

Supported objective types: 1 "Aller voir #1" and 9 "Retourner voir #1" (talk to an NPC on a known map).
NOT supported (the autopilot stops and says why): fights (type 6, 7, 13, 14, 16: the combat protocol is not implemented), crafting,
item hand-ins, map discovery, anything whose NPC map is unknown, and map changes that need an interactive element (doors, zaaps).
It also stops when a fight starts, when the character disconnects, after MAX_ATTEMPTS failed tries on the same objective, or on request.

Travel uses %APPDATA%/dofus_net/mapgraph.json (tools/index_maps.py): {map id: [top, bottom, left, right] neighbour ids}."""
import collections, json, os, re, threading, time
import gamedata, gamequests, store

GRAPH_PATH = os.path.join(os.path.dirname(store.PATH), "mapgraph.json")
SIDES = ("top", "bottom", "left", "right")
TALK_TYPES = {1, 9}
FIGHT_TYPES = {6, 7, 13, 14, 16}
MAX_ATTEMPTS = 3
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
    """Shortest sequence of sides ('top'/'bottom'/'left'/'right') from map `start` to map `goal`, or None."""
    g = graph(); start, goal = int(start), int(goal)
    if start == goal: return []
    prev = {start: None}; queue = collections.deque([start])
    while queue:
        cur = queue.popleft()
        for side, nxt in zip(SIDES, g.get(cur, ())):
            if not nxt or nxt in prev: continue
            prev[nxt] = (cur, side)
            if nxt == goal:
                out = []
                while prev[nxt]: nxt, side = prev[nxt][0], prev[nxt][1]; out.append(side)
                return out[::-1]
            queue.append(nxt)
    return None


def pick_reply(replies):
    for r in replies:
        if not REFUSE.search(r["text"]): return r
    return replies[0]


class Autopilot:
    def __init__(self, sess):
        self.s = sess; self.on = False; self.goal = None
        self._stop = threading.Event(); self._thread = None; self.attempts = collections.Counter()

    # ---- control ----
    def start(self):
        if self.on: return False
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
                if self.s.in_fight: self.say("warn", "un combat est en cours : la gestion des combats n'est pas implémentée, arrêt"); break
                if self.s.dialog: self._dialog(); continue
                obj, why = self._pick()
                if obj is None: self.say("info", why); break
                if not self._objective(*obj): break
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
                text = gamequests.objective_text(oid)
                if o.get("type") in TALK_TYPES and o.get("map"):
                    if self.attempts[oid] < MAX_ATTEMPTS: return (qid, oid), None
                    unsupported = unsupported or f"objectif « {text} » : échec après {MAX_ATTEMPTS} essais"
                elif o.get("type") in FIGHT_TYPES: unsupported = unsupported or f"objectif « {text} » : il faut un combat (non géré)"
                else: unsupported = unsupported or f"objectif « {text} » : type non géré"
        return None, ("aucun objectif géré — " + unsupported) if unsupported else "aucune quête en cours"

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
        for _ in range(MAX_TRAVEL_STEPS):
            cur = self.s.map_id
            if not self._alive(): return False
            if cur == goal: return True
            r = route(cur, goal)
            if r is None: self.say("error", f"aucune route de la carte {cur} à la carte {goal} (porte, zaap ou carte inconnue ?)"); return False
            side = r[0]
            self.say("info", f"voyage vers {goal} : {len(r)} carte(s), prochaine sortie {side}")
            try: self._act(f"Changer de carte ({side})", lambda: self.s._do_change_map(side))
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
