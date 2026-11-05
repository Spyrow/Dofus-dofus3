"""Fight engine (first version, from two captures of level-1 fights against single monsters).

Flow observed (obfuscated names, see login.py):
  attack a map group:  hms {2: group actor}  -> server loads the fight map (jow) -> client igk -> server jxi (placement) -> client jxj {1: 1} (ready)
  my turn:             server jvi, jti, jvj {2: duration, 3: actor}, jte  -> I may play (jvj.3 == me)
  actions:             jsr {1: spell id, 2: cell} (cast), jok {1: path keys, 2: fight map id} (move), jwo (end turn)
  acknowledgements:    jsy {2: id, 3?, 4: actor} -> jst {2: id, 3: 1} for my actor and ids in ACK_IDS (animation finished);
                       jug {2: actor} -> jtd {1: 1} (every sequence boundary, mine or the monsters')
  end:                 jwp / jwh (results), then the exploration map is sent again (jow).
Movement in a fight is restricted to the 4 diagonal directions (a straight step is refused with jpc -7).
The default engine is fightai.plan (spell effects, zones, resistances, positioning, kills; see that module); the simple engine (cast the first
affordable spell that reaches the nearest enemy, else walk toward it) stays as the fallback when the planner cannot play a turn.
The fight state is followed from the events: jsq actions 91..100 carry the damage ({37: {4: element, 5: target, 6: amount}}, source in 36) and 108 the
heals ({39: {2: target, 4: amount}}), so the life of every fighter is exact. Summons and allies are told from the monsters by the master id of the
fighter entry (3.4.10: 0 for a monster, the summoner for a summon)."""
import threading, time
import gamedata, gamemaps, fightai, challenges
from wire import parse, f_bytes, f_varint, enc_varint

ACK_IDS = {3, 7, 8, 10, 12, 13, 15, 16, 17, 23, 59}   # jsy ids the real client acknowledged for its own actor
MAX_GROUP = 3
TURN_TIMEOUT = 60
FIGHT_TIMEOUT = 600
AI_ENABLED = True      # the planner of fightai.py; False (or any failure of it) plays with the simple engine below
HIT_DEFAULT = 0.35     # damage one hit of an unknown monster is guessed to do, as a share of its life
RESUME_IDLE_TIMEOUT = 20


def s64(v):
    return None if v is None else (v - (1 << 64) if v >= 1 << 63 else v)


def _vi(body, num):
    try: return next((v for f, v in parse(body) if f == num and isinstance(v, int)), None)
    except (ValueError, IndexError): return None


def monster_group(entry):
    """Map actor entry of a monster group -> [monster template ids] ([] if it is not a group). Group data: 3.1.5.6 with the
    leader in field 4 and the others in repeated field 3, each carrying its template id in field 5."""
    def sub(b, *path):
        try:
            for n in path: b = next(v for f, v in parse(b) if f == n and isinstance(v, bytes))
            return b
        except (StopIteration, ValueError, IndexError, TypeError): return None
    g = sub(entry, 3, 1, 5, 6)
    if g is None: return []
    out = []
    try:
        for f, v in parse(g):
            if f in (3, 4) and isinstance(v, bytes):
                mid = _vi(v, 5)
                if mid: out.append(mid)
    except (ValueError, IndexError): pass
    return out


def los_clear(m, a, b):
    """True if no cell strictly between a and b blocks the line of sight (cells[i][1] == 0). Unknown map -> True."""
    if not m: return True
    (ax, ay), (bx, by) = gamemaps.to_xy(a), gamemaps.to_xy(b)
    n = max(abs(bx - ax), abs(by - ay))
    for i in range(1, n):
        c = gamemaps.from_xy(round(ax + (bx - ax) * i / n), round(ay + (by - ay) * i / n))
        if c is not None and not m["cells"][c][1]: return False
    return True


def dist(a, b):
    (ax, ay), (bx, by) = gamemaps.to_xy(a), gamemaps.to_xy(b)
    return abs(ax - bx) + abs(ay - by)


class Fight:
    HIT = {}      # monster template -> damage of one hit on me (moving average, learned from the damage events, kept across fights)
    REACH = {}    # monster template -> distance its hits on me come from (see _track_life)
    REACH_SEEN = {}

    def __init__(self, sess):
        self.s = sess; self.spell_score = {}; self.mem = fightai.Memory(); self.mem.reliability = self.spell_score
        self.chal = challenges.State(); self.chal_prefs = {}; self.chal_log = []; self.ctx = None      # fight challenges (client/challenges.py): kept across fights
        self.reset()

    def reset(self):
        self.started, self.over, self.my_turn, self.cast_evt = (threading.Event() for _ in range(4))
        self.jvj_me = False; self.dead = set(); self.casts = {}
        self.tpl, self.hp, self.pm, self.mutilated = {}, {}, {}, False; self.kite_focus = None      # fighter -> monster template / life / MP; Mutilation already cast this fight
        self.master, self.res, self.hp_max, self.my_hp, self.turn_no = {}, {}, {}, None, 0; self.mem.new_fight()
        self.focus = None; self.mp_spent = 0      # Focus challenge: the enemy being attacked; movement points spent in this turn
        self.last_message_at = 0.0; self.ap_err = None      # ap_err: (AP I have, AP the spell needs, time) from the server's "not enough AP" answer
        if not hasattr(self, "cost"): self.cost = {}      # spell id -> the AP cost the server really asked (the data holds the first grade's), kept across fights
        if not hasattr(self, "spell_score"): self.spell_score = {}   # spell id -> [reactions, silences], kept across fights

    def begin(self, ctx):
        """A fight is about to start (the session just sent the attack, or a dialog started it): what it is for decides the challenge bonus."""
        self.ctx = ctx

    def on_challenge_message(self, t, body):
        """A server message the session does not know, during the placement phase: is it a challenge? (read by its shape, client/challenges.py)"""
        kind = self.chal.feed(t, body)
        if kind in ("proposal", "one", "list"):
            shown = self.chal.proposals if kind == "proposal" else self.chal.listed if kind == "list" else [self.chal.active]
            self.s.log("info", f"Défis : message « {t} » = " + ("propositions" if kind == "proposal" else "défi du combat") + " : " + ", ".join(challenges.describe(c) for c in shown if c))
        elif kind == "result":
            self.s.log("event", "Défi " + ("RÉUSSI" if self.chal.result["ok"] else "échoué") + " : " + challenges.describe(self.chal.active or {"id": self.chal.result["id"]}))

    # ---- messages (called from the session's receive loop) ----
    def on_message(self, t, body):
        import login
        s = self.s
        if t == login.JXI: self.reset()
        self.last_message_at = time.monotonic()
        if t == login.JXI: self.started.set()
        elif t in (login.JUO, login.JWJ):
            for f, v in parse(body):
                if f == 1 and isinstance(v, bytes):
                    try: s._register_actor(v); self._note_fighter(v)
                    except (ValueError, IndexError, KeyError): pass
        elif t == login.JVJ: self.jvj_me = s64(_vi(body, 3)) == s.char_id
        elif t == login.JTE:
            if self.jvj_me: self.jvj_me = False; self.my_turn.set()
        elif t == login.JUG: self._later(0.7, login.JTD, f_varint(1, 1))
        elif t == login.JSY:
            sid = _vi(body, 2)
            if s64(_vi(body, 4)) == s.char_id and sid in ACK_IDS:
                self._later(1.0 if _vi(body, 3) is None else 0.4, login.JST, f_varint(2, sid) + f_varint(3, 1))
        elif t == "lof" and _vi(body, 2) == 170:      # lof {2: 170, 4: have, 4: need}: the cast was refused for lack of action points
            args = [v.decode(errors="replace") for f, v in parse(body) if f == 4 and isinstance(v, bytes)]
            if len(args) >= 2 and args[0].isdigit() and args[1].isdigit(): self.ap_err = (min(int(args[0]), int(args[1])), max(int(args[0]), int(args[1])), time.monotonic())      # the need is the bigger of the two
        elif t == login.JTK: self.cast_evt.set()
        elif t == login.JSQ:   # jsq {22: action id, 3: {2: victim}} -- action 103 is a death (seen when the last cast killed the monster)
            self.cast_evt.set()
            try: self._track_life(body)
            except (ValueError, IndexError, KeyError, TypeError): pass
            if _vi(body, 22) == 5:   # slide / charge: {5: {1: actor, 2: previous cell, 3: new cell}} -- the monsters that charge end up next to us
                sub = next((v for f, v in parse(body) if f == 5 and isinstance(v, bytes)), b"")
                who, dest = s64(_vi(sub, 1)), _vi(sub, 3)
                if who is not None and dest is not None and 0 <= dest < 560:
                    with s._lock:
                        s.actors[who] = dest
                        if who == s.char_id: s.my_cell = dest
            if _vi(body, 22) == 103:
                sub = next((v for f, v in parse(body) if f == 3 and isinstance(v, bytes)), b"")
                dead = s64(_vi(sub, 2))
                if dead == s.char_id: s.deaths = getattr(s, "deaths", 0) + 1; s.log("event", f"Combat : DÉFAITE, mon personnage est mort (défaite n°{s.deaths} de cette session)")
                elif dead is not None:
                    self.dead.add(dead)
                    with s._lock: s.actors.pop(dead, None)
                    s.log("event", "Combat : ennemi vaincu")
        elif t == login.JWP: self.over.set()

    def _note_fighter(self, entry):
        """juo / jwj entry -> remember the monster template, life and movement points of a fighter (3.4.6.3.3 template; 3.4.7.4 = stats
        [{2: stat id (absent = life), 3: {2: value}}]: Kardorim has 140 life and 4 MP, a chafer 54 life and 3 MP)."""
        def sub(b, *path):
            try:
                for n in path: b = next(v for f, v in parse(b) if f == n and isinstance(v, bytes))
                return b
            except (StopIteration, ValueError, IndexError, TypeError): return None
        try:
            aid = s64(_vi(entry, 1))
            if aid is None or aid == self.s.char_id: return
            tpl = sub(entry, 3, 4, 6, 3); stats = sub(entry, 3, 4, 7)
            if tpl: self.tpl[aid] = _vi(tpl, 3)
            self.master[aid] = s64(_vi(sub(entry, 3, 4) or b"", 10)) or 0
            for f, v in parse(stats or b""):
                if f != 4 or not isinstance(v, bytes): continue
                sid = _vi(v, 2) or 0; val = _vi(sub(v, 3) or b"", 2)
                if val is None: continue
                if sid == 0: self.hp[aid] = val; self.hp_max[aid] = val
                elif sid == 23: self.pm[aid] = val
                elif sid in (33, 34, 35, 36, 37): self.res.setdefault(aid, {})[{37: 0, 33: 1, 34: 2, 35: 3, 36: 4}[sid]] = s64(val)
        except (ValueError, IndexError): pass

    def _is_enemy(self, aid):
        """A fighter is an enemy when its master chain ends on a monster: monsters and their summons (negative ids); my summons end on me."""
        root = aid
        for _ in range(5):
            m = self.master.get(root, 0)
            if not m: break
            root = m
        return root < 0

    def _enemy_cells(self):
        with self.s._lock: return {a: c for a, c in self.s.actors.items() if a != self.s.char_id and a not in self.dead and self._is_enemy(a)}

    def _track_life(self, body):
        """Follow the life of every fighter from the damage / heal events, and learn what the monsters do to me."""
        act = _vi(body, 22); s = self.s
        sub = lambda n: next((v for f, v in parse(body) if f == n and isinstance(v, bytes)), None)
        if act in fightai.DMG_ACTION or act in fightai.STEAL_ACTION:
            ev = sub(37)
            if ev is None: return
            tgt, amount, src = s64(_vi(ev, 5)), _vi(ev, 6) or 0, s64(_vi(body, 36))
            if tgt == s.char_id:
                if self.my_hp is not None: self.my_hp = max(0, self.my_hp - amount)
                if src is not None and src != s.char_id and self._is_enemy(src):
                    tpl = self.tpl.get(src); cells = dict(s.actors)
                    if tpl is not None:
                        old = Fight.HIT.get(tpl); Fight.HIT[tpl] = amount if old is None else 0.6 * old + 0.4 * amount
                        if src in cells and s.my_cell is not None:      # the reach is the second farthest hit seen (one hit may come from a stale position)
                            seen = Fight.REACH_SEEN.setdefault(tpl, []); seen.append(dist(cells[src], s.my_cell)); del seen[:-8]
                            Fight.REACH[tpl] = min(8, sorted(seen, reverse=True)[1]) if len(seen) > 1 else 1
            elif tgt in self.hp: self.hp[tgt] -= amount
        elif act == fightai.HEAL_ACTION:
            ev = sub(39)
            if ev is None: return
            tgt, amount = s64(_vi(ev, 2)), _vi(ev, 4) or 0
            if tgt == s.char_id:
                if self.my_hp is not None: self.my_hp = min(self.my_hp + amount, self._my_hp_max() or self.my_hp + amount)
            elif tgt in self.hp:
                self.hp[tgt] = min(self.hp[tgt] + amount, self.hp_max.get(tgt, self.hp[tgt] + amount))
                if self._is_enemy(tgt): self.mem.healed.add(tgt)      # a healer: anti-heal spells are worth casting on it

    def _my_hp_max(self): return self.s.stats.get(0)

    def _later(self, delay, name, body):
        def go():
            time.sleep(delay)
            try: self.s._send(name, body)
            except Exception: pass
        threading.Thread(target=go, daemon=True).start()

    # ---- playing ----
    def play(self, resume=False):
        """Run a whole fight (blocking). The fight map is already loaded (the attack request was answered).
        resume: the fight was already running when we connected (reconnection): no placement / ready, just wait for our turns."""
        import login
        s = self.s; say = lambda m: s.log("action", "Combat : " + m)
        if resume: say("reprise du combat en cours")
        else:
            s._send(login.IGK); say("placement (igk)")   # jxi may already have arrived (it resets the state itself): do not reset here
            if not self.started.wait(10): raise RuntimeError("le combat n'a pas démarré (pas de jxi)")
            time.sleep(1.0)
            self._placement_challenges()
            s._send(login.JXJ, f_varint(1, 1)); say("prêt (jxj)")
        end = time.time() + FIGHT_TIMEOUT
        while not self.over.is_set() and time.time() < end and s.status == "connected" and not s._stop.is_set():
            if resume and time.monotonic() - self.last_message_at >= RESUME_IDLE_TIMEOUT:
                raise RuntimeError(f"aucun message de combat depuis {RESUME_IDLE_TIMEOUT} s")
            if not self.my_turn.wait(1.0): continue
            self.my_turn.clear()
            time.sleep(0.8)   # the start-of-turn acknowledgements go out first
            self.turn_no += 1
            if self.over.is_set(): break
            try: self._turn()
            except RuntimeError as e: say(f"tour interrompu : {e}")
            if self.over.is_set(): break   # the last cast ended the fight: no turn to end
            s._send(login.JWO); say("fin de tour (jwo)")
        if not self.over.wait(15): raise RuntimeError("fin du combat non reçue")
        say("terminé"); time.sleep(3)
        self._challenge_outcome()

    def _score(self, spell_id, reacted):
        ok, fail = self.spell_score.get(spell_id, [0, 0]); self.spell_score[spell_id] = [ok + bool(reacted), fail + (not reacted)]

    def _rank(self, sp):
        ok, fail = self.spell_score.get(sp["id"], [0, 0])
        return (fail / (ok + fail) if ok + fail else 0.0, -ok)

    def _spells(self):
        out = []
        for sid in self.s.spell_ids:
            sp = gamedata.get("spells", sid); lv = [gamedata.get("spell_levels", i) for i in (sp or {}).get("levels", [])]
            lv = min((x for x in lv if x), key=lambda x: x["grade"], default=None)
            if lv: out.append({"id": sid, "name": sp["name"], "ap": self.cost.get(sid, lv["ap"]), "min": lv["min_range"], "max": lv["range"], "per_turn": lv["max_per_turn"]})
        return out

    # ---- boss tactic (a big group: Kardorim and his chafers) ----
    # Mutilation (self buff, once), then Stase (1-5 cells, steals life) at the boss from a distance, then run away with the remaining MP so that the
    # monsters have to walk to reach us (a Sacrieur with ~100 life does not survive a melee against 8 enemies).
    KITE_MIN_ENEMIES = 4

    def _kite_spells(self):
        by = {sp["name"]: sp for sp in self._spells()}
        return by.get("Mutilation"), by.get("Stase")

    def _threat(self, cell, enemies):
        """How many enemies can reach a cell next to `cell` with their movement points."""
        return sum(1 for a, c in enemies.items() if dist(cell, c) <= self.pm.get(a, 3) + 1)

    def _reachable(self, me, mp, blocked, m):
        """Cells reachable in at most `mp` diagonal steps (the only steps allowed in a fight) -> {cell: path}."""
        out = {me: [me]}; frontier = [me]
        for _ in range(mp):
            nxt = []
            for c in frontier:
                x, y = gamemaps.to_xy(c)
                for i, (dx, dy) in enumerate(gamemaps.DIRS):
                    if not i % 2: continue
                    n = gamemaps.from_xy(x + dx, y + dy)
                    if n is None or n in out or n in blocked or not gamemaps.walkable(m, n) or not gamemaps.can_step(m, c, n): continue
                    out[n] = out[c] + [n]; nxt.append(n)
            frontier = nxt
        return out

    def _boss(self, enemies):
        """The enemy with the most life (the boss: 140 against 54 for a chafer)."""
        return max(enemies, key=lambda a: self.hp.get(a, 0)) if enemies else None

    def _best_cell(self, me, mp, enemies, blocked, m, boss, want_range=None):
        """Cell to end the movement on: the fewest enemies able to reach it, then the farthest from all of them; with `want_range` (Stase's range)
        the cells from which the boss is in range with a clear line come first."""
        best = None
        for c, path in self._reachable(me, mp, blocked, m).items():
            near = min((dist(c, e) for e in enemies.values()), default=9)
            inr = want_range and want_range[0] <= dist(c, enemies[boss]) <= want_range[1] and los_clear(m, c, enemies[boss])
            key = (-self._threat(c, enemies), bool(inr) if want_range else 0, min(near, 8), -len(path))
            if best is None or key > best[0]: best = (key, c, path)
        return best

    def _move(self, path, say, why):
        import login
        s = self.s
        s._moved.clear(); t0 = time.time(); keys = gamemaps.movement_keys(path)
        s._send(login.JOK, f_bytes(1, b"".join(enc_varint(k) for k in keys)) + f_varint(2, s.map_id))
        say(f"{why} : de {path[0]} à {path[-1]} ({len(path) - 1} case(s))")
        if not s._moved.wait(3):
            got = sorted({str(t) for ts, t, _b in s.recent if ts >= t0 and t not in ("knb", None)})
            raise RuntimeError(f"déplacement refusé (le serveur a répondu : {', '.join(got) or 'rien'})")
        self.mp_spent += len(path) - 1
        time.sleep(gamemaps.STEP_SECONDS * (len(path) - 1) + 0.6)

    def _wait_cast(self, sp):
        """Wait for the reaction to a cast just sent. -> (reacted, ap left or None): the server's \"not enough AP\" answer ends the wait at once and
        tells the real balance, which also corrects the cost the data gave for that spell."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < 3:
            if self.cast_evt.is_set(): return True, None
            e = self.ap_err
            if e and e[2] >= t0 - 0.05:
                self.cost[sp["id"]] = max(e[1], sp["ap"]); sp["ap"] = self.cost[sp["id"]]
                return False, e[0]
            time.sleep(0.05)
        return self.cast_evt.is_set(), None

    def _cast(self, sp, cell, say, note=""):
        import login
        self.cast_evt.clear()
        self.s._send(login.JSR, f_varint(1, sp["id"]) + f_varint(2, cell)); say(f"{sp['name']} sur la case {cell}{note}")
        ok, _left = self._wait_cast(sp); self._score(sp["id"], ok)
        if ok: time.sleep(1.3)
        return ok

    def _kite_turn(self, ap, mp, mut, stase):
        """Turn 1: Mutilation, then every turn Epee Vorace (1-3 cells, 3 PA) -- Stase (1-5) when it is out of reach -- always on the SAME monster
        (self.kite_focus: the nearest one at first, kept until the server announces its death), walking up to it with the MP left."""
        s = self.s; say = lambda m: s.log("action", "Combat : " + m)
        sword = next((sp for sp in self._spells() if sp["name"].casefold().startswith("épée vorace")), None)
        skipped = set(); casts = {}
        for _ in range(14):
            with s._lock: me = s.my_cell
            enemies = self._enemy_cells()
            if me is None or not enemies or self.over.is_set(): return
            m = s.mapdata()
            blocked = {c for a, c in s.actors.items() if a != s.char_id}
            # 1. Mutilation once per fight (casting it again would remove it)
            if not self.mutilated and ap >= mut["ap"]:
                self.mutilated = True
                if self._cast(mut, me, say): ap -= mut["ap"]
                continue
            # 2. the focus: the same monster until it dies
            if self.kite_focus not in enemies: self.kite_focus = min(enemies, key=lambda a: (dist(me, enemies[a]), a)); say(f"cible unique : {self.kite_focus}")
            tgt = self.kite_focus; tc = enemies[tgt]
            for sp in [x for x in (sword, stase) if x]:
                if ap >= sp["ap"] and (not sp["per_turn"] or casts.get(sp["id"], 0) < sp["per_turn"]) and (tgt, sp["id"]) not in skipped                         and sp["min"] <= dist(me, tc) <= sp["max"] and los_clear(m, me, tc):
                    if self._cast(sp, tc, say, f" (cible unique {tgt})"): ap -= sp["ap"]; casts[sp["id"]] = casts.get(sp["id"], 0) + 1
                    else: skipped.add((tgt, sp["id"]))
                    break
            else:
                # out of reach: walk toward the focus (diagonal steps), stopping as soon as a spell reaches it
                if mp <= 0 or ap < min(x["ap"] for x in (sword, stase) if x): return
                reach = max(x["max"] for x in (sword, stase) if x)
                path = gamemaps.find_path(m, me, tc, blocked, diagonals_only=True)
                steps = []
                for c in (path or [])[1:-1][:mp]:
                    steps.append(c)
                    if dist(c, tc) <= max(1, min(reach, 3)) and los_clear(m, c, tc): break
                if not steps: return
                try: self._move([me] + steps, say, f"approche de la cible {tgt}")
                except RuntimeError as e: say(f"déplacement refusé ({e})"); return
                mp -= len(steps)

    # ---- planner (fightai) ----
    def _board(self, ap, mp):
        s = self.s
        with s._lock: me = s.my_cell; actors = dict(s.actors)
        if me is None: return None
        stats = dict(s.stats); hp_max = stats.get(0) or 0
        if self.my_hp is None: self.my_hp = max(1, hp_max - abs(stats.get(97) or 0)) if hp_max else 1
        enemies, allies = {}, {}
        for a, c in actors.items():
            if a == s.char_id or a in self.dead: continue
            if not self._is_enemy(a): allies[a] = c; continue
            tpl = self.tpl.get(a); hp0 = self.hp_max.get(a) or self.hp.get(a) or 30
            hit = Fight.HIT.get(tpl, max(3.0, HIT_DEFAULT * hp0)) * 1.5
            enemies[a] = fightai.Enemy(a, c, self.hp.get(a, hp0), hp0, self.res.get(a, {}), self.pm.get(a, 3), Fight.REACH.get(tpl, 1), hit)
        return fightai.Board(s.mapdata(), me, ap, mp, self.my_hp, hp_max or self.my_hp, stats, enemies, allies, self.turn_no, self._rules())

    # ---- challenges ----
    def _rules(self):
        """fightai.Rules of the active challenge, None when there is none the planner honours."""
        rule = self.chal.rule
        if rule not in challenges.POSITION_RULES: return None
        if self.focus is not None and (self.focus in self.dead or self.hp.get(self.focus, 1) <= 0): self.focus = None
        return fightai.Rules({rule}, self.focus)

    def _caps(self):
        """What the character can do, for the odds of a challenge: melee = most of its spells reach 2 cells at most."""
        try: ranges = sorted(sp.max for sp in self._ai_spells())
        except Exception: ranges = []
        return {"melee": bool(ranges) and ranges[len(ranges) // 2] <= 2}

    def _wait_for(self, pred, secs):
        end = time.monotonic() + secs
        while time.monotonic() < end:
            if pred(): return True
            time.sleep(0.1)
        return bool(pred())

    def _placement_challenges(self):
        """Placement phase, just before the ready: set the bonus (XP / loot) and pick the challenge when the server offers a choice. Never raises."""
        s = self.s; st = self.chal; say = lambda m: s.log("action", m)
        ctx = self.ctx or challenges.Context(); ctx.n_enemies = len(self._enemy_cells()) or ctx.n_enemies
        res = {}
        try: res = challenges.place(st, ctx, challenges.settings(s.aid), challenges.Names(), self.chal_prefs, s._send, self._wait_for, say, self._caps())
        except Exception as e: say(f"Défis : ignorés ({e!r})")
        act = st.active
        if act:
            i = challenges.info(act["id"]); rule = st.rule
            how = ("je le respecte" if rule in challenges.POSITION_RULES else "rien à adapter en solo" if rule in ("free", "order") else "je ne sais pas le respecter")
            say(f"Défis : défi du combat : {challenges.describe(act)}" + (f" — {i['desc']}" if i else "") + f" → {how}")
        elif st.listening() and not st.seen: say("Défis : aucun défi annoncé pour ce combat")
        self.chal_log.append({"t": round(time.time()), "purpose": ctx.purpose, "monsters": ctx.monsters, "bonus": res.get("bonus"), "why": res.get("reason"),
                              "proposed": [challenges.describe(c) for c in st.proposals], "chosen": res.get("chosen"), "active": challenges.describe(act) if act else None,
                              "rule": st.rule, "sent": res.get("sent"), "result": None})
        del self.chal_log[:-20]

    def _challenge_outcome(self):
        """End of the fight: the result the server announced for the challenge."""
        r = self.chal.result
        if self.chal_log and self.chal_log[-1]["result"] is None and self.chal.active_id:
            self.chal_log[-1]["result"] = None if r is None else ("réussi" if r["ok"] else "échoué")
        if r is None and self.chal.active_id: self.s.log("info", "Défis : résultat du défi non reçu (message de résultat non reconnu)")

    def _rule_moves(self, rule, mp_total):
        """Zombie (exactly one movement point per turn) and Nomade (every movement point): spend what the plan did not, in one walk."""
        if rule not in ("move1", "moveall") or self.over.is_set(): return
        need = (1 if rule == "move1" else mp_total) - self.mp_spent
        if need <= 0: return
        s = self.s; say = lambda m: s.log("action", "Combat : " + m)
        with s._lock: me = s.my_cell
        if me is None: return
        blocked = {c for a, c in s.actors.items() if a != s.char_id}
        exact = {c: p for c, p in fightai.reachable(s.mapdata(), me, need, blocked).items() if len(p) - 1 == need}
        name = "Zombie" if rule == "move1" else "Nomade"
        if not exact: say(f"défi {name} : impossible de dépenser {need} PM de plus"); return
        board = self._board(0, 0); alive = list(board.enemies) if board else []
        rng = max((sp.max for sp in self._ai_spells()), default=1)
        key = lambda c: (board is None or fightai.position_ok(c, board, alive), fightai.position_score(c, board, alive, rng) if board else 0)
        cell = max(exact, key=key)
        try: self._move(exact[cell], say, "défi " + name + (" : un PM exactement" if rule == "move1" else " : tous les PM"))
        except RuntimeError as e: say(f"défi : déplacement refusé ({e})")

    def _ai_spells(self):
        out = []
        for sid in self.s.spell_ids:
            sp = fightai.load_spell(sid, self.s.level, self.cost.get(sid))
            if sp: out.append(sp)
        return out

    def _turn_ai(self, ap, mp):
        """Play one turn with the planner, then the movement the challenge asks for -> False when the planner cannot (no spell data): the simple engine plays."""
        rule = self.chal.rule; mp_total = mp
        if rule == "stay": mp = 0          # Statue: the turn ends where it began
        elif rule == "move1": mp = min(mp, 1)      # Zombie
        ok = self._turn_ai_loop(ap, mp)
        if ok: self._rule_moves(rule, mp_total)
        return ok

    def _turn_ai_loop(self, ap, mp):
        s = self.s; say = lambda m: s.log("action", "Combat : " + m)
        spells = self._ai_spells()
        if not spells: return False
        import login
        mem = self.mem; mem.new_turn(); skipped = set(); moves = 0
        for _ in range(20):
            if self.over.is_set(): return True
            board = self._board(ap, mp)
            if board is None or not board.enemies: return True
            plan = fightai.plan(board, spells, mem, skipped)
            if plan is None: return True
            if len(plan.dest_path) > 1 and moves < 3:
                try: self._move(plan.dest_path, say, f"IA : position ({plan.note})")
                except RuntimeError as e: say(f"IA : déplacement refusé ({e}), je joue sur place"); mp = 0; moves = 9; continue
                mp -= len(plan.dest_path) - 1; moves += 1; continue
            if plan.casts:
                sp, cell = plan.casts[0]; d = {"id": sp.id, "name": sp.name, "ap": sp.ap}
                victim = next((a for a, c in self._enemy_cells().items() if c == cell), None)
                self.cast_evt.clear(); s._send(login.JSR, f_varint(1, sp.id) + f_varint(2, cell)); say(f"IA : {sp.name} sur la case {cell}" + (f" (ennemi {victim}, vie {self.hp.get(victim)})" if victim else ""))
                ok, left = self._wait_cast(d)
                if left is not None:      # the server's answer also corrected the cost of this spell (self.cost): use it for the rest of the turn
                    ap = left; sp.ap = d["ap"]; say(f"pas assez de PA pour « {sp.name} » : il m'en reste {ap}"); continue
                self._score(sp.id, ok)
                if not ok: skipped.add((sp.id, cell)); say(f"pas de réaction à « {sp.name} » sur la case {cell} : écarté pour ce tour"); continue
                ap -= sp.ap; mem.casts_turn[sp.id] = mem.casts_turn.get(sp.id, 0) + 1; mem.last_cast[sp.id] = self.turn_no
                if victim: mem.target_casts[(sp.id, victim)] = mem.target_casts.get((sp.id, victim), 0) + 1
                if victim and self.chal.rule == "focus" and self.focus is None: self.focus = victim      # Focus challenge: this one first, to the end
                for f in sp.fx:
                    if f.kind == "buff": mem.buff_until[sp.id] = self.turn_no + (f.duration if 0 < f.duration < 50 else 999)
                time.sleep(1.3); continue
            if len(plan.end_path) > 1 and moves < 5:
                try: self._move(plan.end_path, say, "IA : repli / approche")
                except RuntimeError as e: say(f"IA : déplacement refusé ({e})")
            return True
        return True

    def _turn(self):
        """One of my turns. A cast that gets no reaction only skips that target FOR THIS TURN (line of sight, a moved enemy...); an enemy
        is only ever removed for good when the server announces its death."""
        import login
        s = self.s; say = lambda m: s.log("action", "Combat : " + m)
        ap, mp = s.stats.get(1, 6), s.stats.get(23, 3); self.mp_spent = 0
        spells = self._spells(); casts = {}; skipped = set(); moves_without_cast = 0
        say(f"mon tour ({ap} PA, {mp} PM)")
        mut, stase = self._kite_spells()
        n_enemies = len(self._enemy_cells())
        dungeon = getattr(getattr(self, "ctx", None), "purpose", None) == "dungeon"      # in a dungeon room: focus one monster at a time to the very end
        if mut and stase and (n_enemies >= self.KITE_MIN_ENEMIES or dungeon):
            say(f"tactique du boss : {n_enemies} ennemis, Mutilation puis Stase à distance puis fuite")
            return self._kite_turn(ap, mp, mut, stase)
        if AI_ENABLED:
            try:
                if self._turn_ai(ap, mp): return
            except RuntimeError: raise
            except Exception as e: say(f"IA avancée en échec ({e!r}) : repli sur l'IA simple")
        for _ in range(14):
            with s._lock: me = s.my_cell
            enemies = self._enemy_cells()
            if me is None or not enemies or self.over.is_set(): return
            usable = [sp for sp in spells if sp["ap"] <= ap and (not sp["per_turn"] or casts.get(sp["id"], 0) < sp["per_turn"])]
            usable.sort(key=self._rank)   # spells that have reacted before come first; silent ones last
            order = sorted(enemies, key=lambda a: dist(me, enemies[a]))
            m = s.mapdata()
            # 1. cast at the nearest enemy that a spell reaches (range + line of sight)
            target = next(((a, sp) for a in order for sp in usable if (a, sp["id"]) not in skipped
                           and sp["min"] <= dist(me, enemies[a]) <= sp["max"] and los_clear(m, me, enemies[a])), None)
            if target:
                tid, sp = target; tcell = enemies[tid]; self.cast_evt.clear()
                s._send(login.JSR, f_varint(1, sp["id"]) + f_varint(2, tcell)); say(f"{sp['name']} sur la case {tcell}")
                ok, ap_left = self._wait_cast(sp)
                if ap_left is not None: ap = ap_left; say(f"pas assez de PA pour « {sp['name']} » : il m'en reste {ap}"); continue
                if not ok:
                    skipped.add((tid, sp["id"])); self._score(sp["id"], False)
                    say(f"pas de réaction de {tid} à « {sp['name']} » ce tour-ci : ce sort est écarté pour cette cible jusqu'à la fin du tour"); continue
                self._score(sp["id"], True)
                ap -= sp["ap"]; casts[sp["id"]] = casts.get(sp["id"], 0) + 1; moves_without_cast = 0; time.sleep(1.3); continue
            if not usable: return
            # 2. walk toward the nearest enemy (diagonal steps only)
            tid = order[0]; tcell = enemies[tid]
            if mp > 0 and moves_without_cast < 3:
                rng = max(sp["max"] for sp in usable)
                blocked = {c for a, c in s.actors.items() if a != s.char_id}
                path = gamemaps.find_path(m, me, tcell, blocked, diagonals_only=True)
                steps = []
                for c in (path or [])[1:-1][:mp]:
                    steps.append(c)
                    if dist(c, tcell) <= max(1, rng - 2): break   # a margin: stop a bit inside the range so that the line of sight is likelier
                if steps:
                    walk = [me] + steps; s._moved.clear(); t0 = time.time(); keys = gamemaps.movement_keys(walk)
                    s._send(login.JOK, f_bytes(1, b"".join(enc_varint(k) for k in keys)) + f_varint(2, s.map_id))
                    say(f"déplacement de {len(steps)} case(s) vers {tid} : de {me} à {steps[-1]}")
                    if not s._moved.wait(3):
                        got = sorted({str(t) for ts, t, _b in s.recent if ts >= t0 and t not in ("knb", None)})
                        raise RuntimeError(f"déplacement refusé (le serveur a répondu : {', '.join(got) or 'rien'})")
                    mp -= len(steps); moves_without_cast += 1; time.sleep(gamemaps.STEP_SECONDS * len(steps) + 0.6); continue
            # 3. last resort: let the server judge a cast in range even if my line-of-sight estimate says no
            blind = next(((a, sp) for a in order for sp in usable if (a, sp["id"]) not in skipped and sp["min"] <= dist(me, enemies[a]) <= sp["max"]), None)
            if blind:
                tid, sp = blind; self.cast_evt.clear()
                s._send(login.JSR, f_varint(1, sp["id"]) + f_varint(2, enemies[tid])); say(f"{sp['name']} sur la case {enemies[tid]} (sans ligne de vue estimée)")
                ok, ap_left = self._wait_cast(sp)
                if ap_left is not None: ap = ap_left; continue
                if not ok: skipped.add((tid, sp["id"])); self._score(sp["id"], False); continue
                self._score(sp["id"], True)
                ap -= sp["ap"]; casts[sp["id"]] = casts.get(sp["id"], 0) + 1; time.sleep(1.3); continue
            return
