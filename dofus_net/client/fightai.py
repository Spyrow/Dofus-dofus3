"""Combat planner: chooses where to stand, which spells to cast on which cell and where to end the turn. Pure logic (no network): fightbot.Fight
builds a Board from the live fight state and plays the plan returned by plan().

What it models (all read from the game's own data, see tools/export_gamedata.py, spell_levels[...]["fx"]):
  - the spell's real effects: damage / life steal per element (dice, the caster's characteristic and power), heals, pushes and pulls, summons, buffs
    and debuffs, each with its target mask (A enemies, a allies, g allies but the caster, C the caster) and its zone shape (point, circle, cross, line...)
  - the monsters' resistances per element (percent), their life (tracked from the damage/heal events), their movement points and the damage they
    were seen to deal (learned, per monster template, across fights)
  - range, minimum range, line of sight, casts per turn / per target, cooldowns, initial cooldowns, action points
  - positions: every cell reachable with the movement points (diagonal steps only in a fight), cast from the best one, then end the turn on the
    cell where the fewest enemies can reach us; the cast is weighted against the danger of the final cell (more when the life is low)
Targets are scored by damage dealt (capped by the life left), a bonus for a kill that grows with the threat of the victim (focus fire, then the most
dangerous first), and a penalty for hurting allies or ourselves. Sequences of casts are searched with a small beam search, so an area spell
that catches three monsters beats three single casts, and a finishing cast is chosen over a stronger one on a monster that is already dead.
Unknown effects are never cast on purpose (value 0); a spell the server ignores is dropped for the turn by the caller."""
import gamedata, gamemaps

ELEMENT_STAT = {0: 10, 1: 10, 2: 15, 3: 13, 4: 14}      # neutral / earth: strength (10), fire: intelligence (15), water: chance (13), air: agility (14)
RESIST_STAT = {0: 37, 1: 33, 2: 34, 3: 35, 4: 36}       # percent resistance characteristics of a fighter, by element
DMG_ACTION = {100: 0, 97: 1, 99: 2, 96: 3, 98: 4}       # action id -> element of a damage effect
STEAL_ACTION = {95: 0, 92: 1, 94: 2, 91: 3, 93: 4}      # life steal: damages and heals the caster by half
HEAL_ACTION = 108
PUSH_ACTION, PULL_ACTION = 5, 6
SUMMON_ACTIONS = {180, 181, 185}
BUFF_ACTIONS = {105, 111, 112, 115, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126, 128, 138, 142, 160, 161, 165, 182, 210, 211, 212, 213, 214, 215, 216, 217, 218, 219, 220, 265}
DEBUFF_ACTIONS = {101, 116, 127, 168, 169, 145, 152, 153, 154, 155, 156, 157, 171, 173, 174, 175, 177, 178, 179}
MAP_SHAPES = {97, 65}            # 'a' / 'A': the whole map
MAX_CASTS = 6
BEAM = 8
PER_SPELL_CELLS = 7             # target cells kept per (position, spell) after a static pre-ranking
MAX_DESTS = 14
SUMMON_VALUE = 9.0
BUFF_VALUE = 5.0
DEBUFF_VALUE = 3.0


class Fx:
    __slots__ = ("kind", "element", "avg", "enemy", "ally", "self_", "shape", "p1", "p2", "duration", "action")

    def __init__(self, row):
        action, element, num, side, mask, shape, p1, p2, duration, _value, delay = row
        self.action, self.shape, self.p1, self.p2, self.duration = action, shape, p1, p2, duration
        toks = {t for t in str(mask).replace(" ", "").split(",") if t and not t.startswith("*")}
        self.enemy = "A" in toks or not toks
        self.ally = "a" in toks or "g" in toks
        self.self_ = "a" in toks or "C" in toks or "c" in toks
        self.element = element if element in ELEMENT_STAT else DMG_ACTION.get(action, STEAL_ACTION.get(action, 0))
        self.avg = (num + side) / 2.0 if side > 0 else float(num)      # "12 à 14" = diceNum..diceSide; a lone value is diceNum
        if delay: self.kind = "other"          # delayed effects (bombs, glyph ticks...) are not modelled
        elif action in DMG_ACTION: self.kind, self.element = "dmg", DMG_ACTION[action]
        elif action in STEAL_ACTION: self.kind, self.element = "steal", STEAL_ACTION[action]
        elif action == HEAL_ACTION: self.kind = "heal"
        elif action == PUSH_ACTION: self.kind = "push"
        elif action == PULL_ACTION: self.kind = "pull"
        elif action in SUMMON_ACTIONS: self.kind = "summon"
        elif action in BUFF_ACTIONS and (self.ally or self.self_) and not self.enemy: self.kind = "buff"
        elif action in DEBUFF_ACTIONS and self.enemy and not self.ally: self.kind = "debuff"
        else: self.kind = "other"


class Spell:
    """A spell as the planner sees it. Geometry and cost come from the first grade (what the bot has always used, corrected by the server's answers);
    the effects come from the best grade the character's level allows (damage grows with it)."""
    def __init__(self, sid, name, ap, lo, hi, per_turn, per_target, cooldown, init_cd, crit, fx, states):
        self.id, self.name, self.ap, self.min, self.max = sid, name, ap, lo, hi
        self.per_turn, self.per_target, self.cooldown, self.init_cd, self.crit, self.fx, self.states = per_turn, per_target, cooldown, init_cd, crit, fx, states
        self.useful = any(f.kind != "other" for f in fx)


def load_spell(sid, level, ap_override=None):
    sp = gamedata.get("spells", sid)
    if not sp: return None
    lvls = [x for x in (gamedata.get("spell_levels", i) for i in sp.get("levels", [])) if x]
    if not lvls: return None
    first = min(lvls, key=lambda x: x["grade"])
    ok = [x for x in lvls if (x.get("min_level") or 0) <= (level or 1)]
    best = max(ok, key=lambda x: x["grade"]) if ok else first
    fx = [Fx(r) for r in best.get("fx", [])]
    return Spell(sid, sp["name"], ap_override or first["ap"], first["min_range"], first["range"], first["max_per_turn"], first.get("per_target", 0),
                 first["cooldown"], first.get("init_cd", 0), best.get("crit", 0), fx, best.get("states") or "")


# ---- geometry ----
def man(a, b):
    (ax, ay), (bx, by) = gamemaps.to_xy(a), gamemaps.to_xy(b)
    return abs(ax - bx) + abs(ay - by)


_zone_cache = {}


def zone(fx, caster, target):
    """Cells covered by an effect aimed at `target` from `caster`. Shapes are the ASCII codes of the game's zone letters; the ones not modelled
    here (cones, half circles...) fall back to the target cell alone."""
    key = (fx.shape, fx.p1, fx.p2, caster if fx.shape == 76 else None, target)
    r = _zone_cache.get(key)
    if r is not None: return r
    tx, ty = gamemaps.to_xy(target); sh, p1, p2 = fx.shape, fx.p1, fx.p2; pts = set()
    if sh in MAP_SHAPES: pts = {c for c in range(gamemaps.CELLS)}
    elif sh == 67:                                              # C circle: Manhattan distance p2..p1
        for dx in range(-p1, p1 + 1):
            for dy in range(-p1, p1 + 1):
                if p2 <= abs(dx) + abs(dy) <= p1: pts.add((tx + dx, ty + dy))
    elif sh == 71:                                              # G square
        for dx in range(-p1, p1 + 1):
            for dy in range(-p1, p1 + 1): pts.add((tx + dx, ty + dy))
    elif sh == 79:                                              # O ring
        for dx in range(-p1, p1 + 1):
            for dy in range(-p1, p1 + 1):
                if abs(dx) + abs(dy) == p1: pts.add((tx + dx, ty + dy))
    elif sh == 88:                                              # X cross along the grid axes
        if p2 == 0: pts.add((tx, ty))
        for k in range(max(1, p2), p1 + 1): pts |= {(tx + k, ty), (tx - k, ty), (tx, ty + k), (tx, ty - k)}
    elif sh == 43:                                              # + diagonal cross
        if p2 == 0: pts.add((tx, ty))
        for k in range(max(1, p2), p1 + 1): pts |= {(tx + k, ty + k), (tx - k, ty - k), (tx + k, ty - k), (tx - k, ty + k)}
    elif sh == 76:                                              # L line from the target away from the caster
        cx, cy = gamemaps.to_xy(caster); dx, dy = tx - cx, ty - cy
        sx, sy = (dx > 0) - (dx < 0), (dy > 0) - (dy < 0)
        for k in range(p2, p1 + 1): pts.add((tx + sx * k, ty + sy * k))
    else: pts = {(tx, ty)}
    out = frozenset(c for c in (p if isinstance(p, int) else gamemaps.from_xy(*p) for p in pts) if c is not None)
    if len(_zone_cache) > 20000: _zone_cache.clear()
    _zone_cache[key] = out
    return out


def los_clear(m, a, b):
    if not m: return True
    (ax, ay), (bx, by) = gamemaps.to_xy(a), gamemaps.to_xy(b)
    n = max(abs(bx - ax), abs(by - ay))
    for i in range(1, n):
        c = gamemaps.from_xy(round(ax + (bx - ax) * i / n), round(ay + (by - ay) * i / n))
        if c is not None and not m["cells"][c][1]: return False
    return True


def reachable(m, me, mp, blocked):
    """Cells reachable in at most mp diagonal steps (the only steps allowed in a fight) -> {cell: path}."""
    out = {me: [me]}; frontier = [me]
    for _ in range(mp):
        nxt = []
        for c in frontier:
            x, y = gamemaps.to_xy(c)
            for i, (dx, dy) in enumerate(gamemaps.DIRS):
                if not i % 2: continue
                n = gamemaps.from_xy(x + dx, y + dy)
                if n is None or n in out or n in blocked or (m and (not gamemaps.walkable(m, n) or not gamemaps.can_step(m, c, n))): continue
                out[n] = out[c] + [n]; nxt.append(n)
        frontier = nxt
    return out


# ---- board ----
class Enemy:
    __slots__ = ("id", "cell", "hp", "hp_max", "res", "mp", "reach", "hit")

    def __init__(self, eid, cell, hp, hp_max, res, mp, reach, hit):
        self.id, self.cell, self.hp, self.hp_max, self.res, self.mp, self.reach, self.hit = eid, cell, hp, hp_max, res, mp, reach, hit

    @property
    def threat(self): return self.hit


class Board:
    """Snapshot of the fight at one point of my turn."""
    def __init__(self, m, me, ap, mp, hp, hp_max, stats, enemies, allies, turn):
        self.m, self.me, self.ap, self.mp, self.hp, self.hp_max, self.stats = m, me, ap, mp, hp, hp_max, stats
        self.enemies = enemies      # {id: Enemy}
        self.allies = allies        # {id: cell} (my summons and companions, not me)
        self.turn = turn
        self.blocked = {e.cell for e in enemies.values()} | set(allies.values())

    @property
    def hp_frac(self): return max(0.05, min(1.0, self.hp / self.hp_max)) if self.hp_max else 1.0


class Memory:
    """What the planner remembers between turns and fights of the same session."""
    def __init__(self):
        self.last_cast = {}         # spell id -> my turn number of the last cast
        self.buff_until = {}        # spell id -> last turn the buff cast is still on
        self.casts_turn = {}        # spell id -> casts this turn
        self.target_casts = {}      # (spell id, target actor) -> casts this turn
        self.reliability = {}       # spell id -> [reactions, silences]

    def new_turn(self): self.casts_turn = {}; self.target_casts = {}

    def new_fight(self): self.last_cast = {}; self.buff_until = {}; self.new_turn()

    def rel(self, sid):
        ok, bad = self.reliability.get(sid, [0, 0])
        return 0.35 + 0.65 * (ok + 1) / (ok + bad + 1)


def damage(fx, stats, enemy):
    """Expected damage of one damage effect on one enemy (average dice, the caster's characteristic + power + flat bonus, then the enemy's resistance)."""
    power = 100 + stats.get(ELEMENT_STAT.get(fx.element, 10), 0) + stats.get(25, 0)
    d = fx.avg * power / 100.0 + stats.get(16, 0)
    res = enemy.res.get(fx.element, 0) if enemy else 0
    return max(0.0, d * (1 - min(res, 90) / 100.0))


def heal_amount(fx, stats): return fx.avg * (100 + stats.get(15, 0)) / 100.0 + stats.get(49, 0)


def usable(sp, board, mem):
    if sp.ap > board.ap or not sp.useful: return False
    if sp.per_turn and mem.casts_turn.get(sp.id, 0) >= sp.per_turn: return False
    if sp.init_cd and board.turn <= sp.init_cd and sp.id not in mem.last_cast: return False
    last = mem.last_cast.get(sp.id)
    if sp.cooldown > 1 and last is not None and board.turn - last < sp.cooldown: return False
    if any(f.kind == "buff" for f in sp.fx) and not any(f.kind in ("dmg", "steal", "heal", "summon", "debuff") for f in sp.fx) and board.turn <= mem.buff_until.get(sp.id, 0): return False
    return True


def candidates(sp, d, board, skipped):
    """Target cells for a spell cast from d -> [(cell, hit enemies, hit allies, self hit)] pre-ranked, at most PER_SPELL_CELLS."""
    m = board.m; out = []
    lo, hi = sp.min, sp.max + (board.stats.get(19, 0) if sp.max > sp.min else 0)
    summon = any(f.kind == "summon" for f in sp.fx)
    wants_enemy = any(f.enemy and f.kind in ("dmg", "steal", "push", "pull", "debuff") for f in sp.fx)
    wants_ally = any((f.ally or f.self_) and f.kind in ("heal", "buff") for f in sp.fx)
    cells = []
    for c in range(gamemaps.CELLS):
        r = man(d, c)
        if lo <= r <= hi and (sp.id, c) not in skipped: cells.append(c)
    for c in cells:
        if c in board.blocked and not (wants_enemy or wants_ally): continue
        if m and not gamemaps.walkable(m, c) and c not in board.blocked and c != board.me: continue
        if not los_clear(m, d, c): continue
        hit_e, hit_a, self_hit = [], [], False
        for f in sp.fx:
            if f.kind == "other": continue
            z = zone(f, d, c)
            for eid, e in board.enemies.items():
                if e.cell in z and f.enemy and eid not in hit_e: hit_e.append(eid)
            for aid, cell in board.allies.items():
                if cell in z and f.ally and aid not in hit_a: hit_a.append(aid)
            if f.self_ and (d in z or (c == d)): self_hit = True
        if summon:
            if c in board.blocked or c == d: continue
        elif not hit_e and not hit_a and not (self_hit and wants_ally): continue
        out.append((c, tuple(hit_e), tuple(hit_a), self_hit))
    out.sort(key=lambda t: (-len(t[1]), -len(t[2]), man(d, t[0])))
    if summon:
        out.sort(key=lambda t: min((man(t[0], e.cell) for e in board.enemies.values()), default=0))
    return out[:PER_SPELL_CELLS]


class Plan:
    __slots__ = ("dest_path", "casts", "end_path", "value", "note")

    def __init__(self, dest_path, casts, end_path, value, note=""):
        self.dest_path, self.casts, self.end_path, self.value, self.note = dest_path, casts, end_path, value, note


def evaluate(sp, cand, d, board, hp, my_hp, mem, n_summons, rel):
    """Value of one cast from d and its effect on the enemies' life -> (value, {enemy id: new hp}, my new life) or None when the cast is worthless.
    Every effect has its own zone and target mask, so the victims are worked out per effect."""
    cell = cand[0]
    value = 0.0; new_hp = dict(hp); me_hp = my_hp; need = max(0.0, board.hp_max - my_hp) if board.hp_max else 0.0
    stats = board.stats; hurt_w = 0.6 + 1.5 * (1 - board.hp_frac)
    for f in sp.fx:
        if f.kind == "other": continue
        z = zone(f, d, cell)
        foes = [eid for eid, e in board.enemies.items() if f.enemy and e.cell in z] if f.kind != "summon" else []
        friends = [a for a, c in board.allies.items() if f.ally and c in z]
        me_in = f.self_ and (d in z)
        if f.kind in ("dmg", "steal"):
            for eid in foes:
                e = board.enemies[eid]
                if new_hp[eid] <= 0: continue
                dm = damage(f, stats, e); real = min(dm, new_hp[eid])
                value += real * (1 + 0.03 * e.threat)
                if dm >= new_hp[eid]: value += 3 + 2.0 * e.threat          # kill: the enemy stops hurting us
                new_hp[eid] -= dm
                if f.kind == "steal":
                    gain = min(real * 0.5, need); value += gain * hurt_w; need -= gain; me_hp += gain
            # friendly fire (a mask with both A and a hurts the allies standing in the zone, and us if we are in it)
            value -= 0.8 * f.avg * len(friends) + (0.8 * f.avg if me_in and f.kind == "dmg" else 0.0)
        elif f.kind == "heal":
            amt = heal_amount(f, stats)
            if me_in and need > 0:
                real = min(amt, need); value += real * hurt_w; need -= real; me_hp += real
        elif f.kind in ("push", "pull"):
            if foes:
                near = sum(1 for eid in foes if man(board.me, board.enemies[eid].cell) <= 2)
                value += 0.4 * len(foes) + (1.5 * near if f.kind == "push" else 0.0)
        elif f.kind == "summon":
            if n_summons < 1 + stats.get(26, 0): value += SUMMON_VALUE
        elif f.kind == "buff":
            if board.turn > mem.buff_until.get(sp.id, 0): value += BUFF_VALUE + 1.0 * len(board.enemies)
        elif f.kind == "debuff":
            value += DEBUFF_VALUE * len(foes)
    if value <= 0.05: return None
    return value * rel, new_hp, me_hp


def danger(cell, board, alive_ids):
    """Expected damage the enemies still alive can inflict on `cell` during their turn (they walk their movement points, then hit within their reach)."""
    return sum(board.enemies[eid].hit for eid in alive_ids if man(board.enemies[eid].cell, cell) <= board.enemies[eid].mp + board.enemies[eid].reach)


def position_score(cell, board, alive_ids, best_range):
    dmg_in = danger(cell, board, alive_ids)
    w = 1.0 + 2.5 * (1 - board.hp_frac)
    score = -dmg_in * w * 0.8
    if alive_ids:
        nearest = min(man(cell, board.enemies[e].cell) for e in alive_ids)
        gap = nearest - (best_range + board.mp)
        if gap > 0: score -= 0.3 * gap          # out of reach for next turn: come closer
    return score


def best_casts(d, board, spells, mem, skipped):
    """Beam search over the casts possible from position d -> (value, [(spell, cell)], enemy hp after, my hp after)."""
    cache = {sp.id: candidates(sp, d, board, skipped) for sp in spells}
    n_summons = len(board.allies)
    start = (0.0, board.ap, (), {eid: e.hp for eid, e in board.enemies.items()}, board.hp, dict(mem.casts_turn), dict(mem.target_casts))
    beam = [start]; best = start
    for _ in range(MAX_CASTS):
        nxt = []
        for val, ap, seq, hp, mhp, cts, tcs in beam:
            for sp in spells:
                if sp.ap > ap or not sp.useful: continue
                if sp.per_turn and cts.get(sp.id, 0) >= sp.per_turn: continue
                if sp.id in mem.last_cast and sp.cooldown > 1 and board.turn - mem.last_cast[sp.id] < sp.cooldown: continue
                if any(s.id == sp.id for s, _c in seq) and any(f.kind in ("buff", "summon") for f in sp.fx) and not any(f.kind in ("dmg", "steal", "heal") for f in sp.fx): continue
                rel = mem.rel(sp.id)
                for cand in cache[sp.id]:
                    if sp.per_target and any(tcs.get((sp.id, e), 0) >= sp.per_target for e in cand[1][:1]): continue
                    if all(hp[e] <= 0 for e in cand[1]) and cand[1] and not cand[2]: continue          # everything it would hit is already dead
                    r = evaluate(sp, cand, d, board, hp, mhp, mem, n_summons + sum(1 for s, _c in seq if any(f.kind == "summon" for f in s.fx)), rel)
                    if r is None: continue
                    v, nhp, nmhp = r
                    v = v / (1 + 0.04 * sp.ap)
                    c2 = dict(cts); c2[sp.id] = c2.get(sp.id, 0) + 1
                    t2 = dict(tcs)
                    if cand[1]: t2[(sp.id, cand[1][0])] = t2.get((sp.id, cand[1][0]), 0) + 1
                    nxt.append((val + v, ap - sp.ap, seq + ((sp, cand[0]),), nhp, nmhp, c2, t2))
        if not nxt: break
        nxt.sort(key=lambda s: -s[0])
        beam = nxt[:BEAM]
        if beam[0][0] > best[0]: best = beam[0]
    return best[0], list(best[2]), best[3], best[4]


def plan(board, spells, mem, skipped=frozenset()):
    """The best (position to cast from, casts, position to end on) for this turn -> Plan or None when nothing is worth doing."""
    if not board.enemies: return None
    spells = [sp for sp in spells if usable(sp, board, mem)]
    reach = reachable(board.m, board.me, board.mp, board.blocked - {board.me})
    best_range = max((sp.max for sp in spells), default=1)

    def cheap(c):       # which positions deserve the full search: close to the targets, not in the middle of the enemies
        near = min(man(c, e.cell) for e in board.enemies.values())
        return (abs(near - min(best_range, 3)), len(reach[c]))
    dests = sorted(reach, key=cheap)[:MAX_DESTS]
    if board.me not in dests: dests.append(board.me)
    best = None
    for d in dests:
        val, casts, hp_after, _mhp = best_casts(d, board, spells, mem, skipped) if spells else (0.0, [], {}, board.hp)
        alive = [eid for eid, h in hp_after.items() if h > 0] if casts else list(board.enemies)
        left = board.mp - (len(reach[d]) - 1)
        ends = reachable(board.m, d, max(0, left), board.blocked - {board.me}) if left > 0 else {d: [d]}
        for end, epath in ends.items():
            score = val + position_score(end, board, alive, best_range) - 0.04 * (len(reach[d]) - 1 + len(epath) - 1)
            if best is None or score > best[0]: best = (score, d, casts, end, epath, val)
    if best is None: return None
    score, d, casts, end, epath, val = best
    if not casts and end == board.me and d == board.me: return None
    return Plan(reach[d], casts, epath, score, f"valeur {val:.1f}, score {score:.1f}")
