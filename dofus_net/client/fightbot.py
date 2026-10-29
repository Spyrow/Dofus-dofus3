"""Fight engine (first version, from two captures of level-1 fights against single monsters).

Flow observed (obfuscated names, see login.py):
  attack a map group:  hms {2: group actor}  -> server loads the fight map (jow) -> client igk -> server jxi (placement) -> client jxj {1: 1} (ready)
  my turn:             server jvi, jti, jvj {2: duration, 3: actor}, jte  -> I may play (jvj.3 == me)
  actions:             jsr {1: spell id, 2: cell} (cast), jok {1: path keys, 2: fight map id} (move), jwo (end turn)
  acknowledgements:    jsy {2: id, 3?, 4: actor} -> jst {2: id, 3: 1} for my actor and ids in ACK_IDS (animation finished);
                       jug {2: actor} -> jtd {1: 1} (every sequence boundary, mine or the monsters')
  end:                 jwp / jwh (results), then the exploration map is sent again (jow).
Movement in a fight is restricted to the 4 diagonal directions (a straight step is refused with jpc -7).
The AI is deliberately simple: cast the first affordable spell that reaches the nearest enemy, else walk toward it, then end the turn.
Line of sight, cooldowns, summons, healing and enemy death are NOT modelled: an enemy that does not react to a cast is dropped from the
target list, and the fight ends on jwp. Only meant for easy fights (see MAX_GROUP)."""
import threading, time
import gamedata, gamemaps
from wire import parse, f_bytes, f_varint, enc_varint

ACK_IDS = {3, 7, 8, 10, 12, 13, 15, 16, 17, 23, 59}   # jsy ids the real client acknowledged for its own actor
MAX_GROUP = 3
TURN_TIMEOUT = 60
FIGHT_TIMEOUT = 600


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
    def __init__(self, sess):
        self.s = sess; self.reset()

    def reset(self):
        self.started, self.over, self.my_turn, self.cast_evt = (threading.Event() for _ in range(4))
        self.jvj_me = False; self.dead = set(); self.casts = {}
        if not hasattr(self, "spell_score"): self.spell_score = {}   # spell id -> [reactions, silences], kept across fights

    # ---- messages (called from the session's receive loop) ----
    def on_message(self, t, body):
        import login
        s = self.s
        if t == login.JXI: self.reset(); self.started.set()
        elif t in (login.JUO, login.JWJ):
            for f, v in parse(body):
                if f == 1 and isinstance(v, bytes):
                    try: s._register_actor(v)
                    except (ValueError, IndexError, KeyError): pass
        elif t == login.JVJ: self.jvj_me = s64(_vi(body, 3)) == s.char_id
        elif t == login.JTE:
            if self.jvj_me: self.jvj_me = False; self.my_turn.set()
        elif t == login.JUG: self._later(0.7, login.JTD, f_varint(1, 1))
        elif t == login.JSY:
            sid = _vi(body, 2)
            if s64(_vi(body, 4)) == s.char_id and sid in ACK_IDS:
                self._later(1.0 if _vi(body, 3) is None else 0.4, login.JST, f_varint(2, sid) + f_varint(3, 1))
        elif t == login.JTK: self.cast_evt.set()
        elif t == login.JSQ:   # jsq {22: action id, 3: {2: victim}} -- action 103 is a death (seen when the last cast killed the monster)
            self.cast_evt.set()
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
                if dead == s.char_id: s.log("event", "Combat : DÉFAITE, mon personnage est mort")
                elif dead is not None:
                    self.dead.add(dead)
                    with s._lock: s.actors.pop(dead, None)
                    s.log("event", "Combat : ennemi vaincu")
        elif t == login.JWP: self.over.set()

    def _later(self, delay, name, body):
        def go():
            time.sleep(delay)
            try: self.s._send(name, body)
            except Exception: pass
        threading.Thread(target=go, daemon=True).start()

    # ---- playing ----
    def play(self):
        """Run a whole fight (blocking). The fight map is already loaded (the attack request was answered)."""
        import login
        s = self.s; say = lambda m: s.log("action", "Combat : " + m)
        s._send(login.IGK); say("placement (igk)")   # jxi may already have arrived (it resets the state itself): do not reset here
        if not self.started.wait(10): raise RuntimeError("le combat n'a pas démarré (pas de jxi)")
        time.sleep(1.0)
        s._send(login.JXJ, f_varint(1, 1)); say("prêt (jxj)")
        end = time.time() + FIGHT_TIMEOUT
        while not self.over.is_set() and time.time() < end and s.status == "connected" and not s._stop.is_set():
            if not self.my_turn.wait(1.0): continue
            self.my_turn.clear()
            time.sleep(0.8)   # the start-of-turn acknowledgements go out first
            if self.over.is_set(): break
            try: self._turn()
            except RuntimeError as e: say(f"tour interrompu : {e}")
            if self.over.is_set(): break   # the last cast ended the fight: no turn to end
            s._send(login.JWO); say("fin de tour (jwo)")
        if not self.over.wait(15): raise RuntimeError("fin du combat non reçue")
        say("terminé"); time.sleep(3)

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
            if lv: out.append({"id": sid, "name": sp["name"], "ap": lv["ap"], "min": lv["min_range"], "max": lv["range"], "per_turn": lv["max_per_turn"]})
        return out

    def _turn(self):
        """One of my turns. A cast that gets no reaction only skips that target FOR THIS TURN (line of sight, a moved enemy...); an enemy
        is only ever removed for good when the server announces its death."""
        import login
        s = self.s; say = lambda m: s.log("action", "Combat : " + m)
        ap, mp = s.stats.get(1, 6), s.stats.get(23, 3)
        spells = self._spells(); casts = {}; skipped = set(); moves_without_cast = 0
        say(f"mon tour ({ap} PA, {mp} PM)")
        for _ in range(14):
            with s._lock: me = s.my_cell; enemies = {a: c for a, c in s.actors.items() if a < 0 and a not in self.dead}
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
                if not self.cast_evt.wait(3):
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
                if not self.cast_evt.wait(3): skipped.add((tid, sp["id"])); self._score(sp["id"], False); continue
                self._score(sp["id"], True)
                ap -= sp["ap"]; casts[sp["id"]] = casts.get(sp["id"], 0) + 1; time.sleep(1.3); continue
            return
