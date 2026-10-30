"""Capital points: spend the "Points de caractéristiques" (characteristic 3) on the six base characteristics.

Message (one capture): client `krc {N: boosts}` spent all 40 points on Chance: `krc {4: 40}` -> characteristic 13 +40, points 40 -> 0.
So the field number identifies the characteristic. Only Chance (13 <-> field 4) has been seen; the others follow the hypothesis
`field = characteristic id - 9` (10 Force, 11 Vitalité, 12 Sagesse, 13 Chance, 14 Agilité, 15 Intelligence) and are CHECKED before use:
the first boost into an unverified characteristic is a single one, and the server's answer (krg) says which characteristic really moved.
A mismatch is recorded (knowledge/boost_fields.json) and the run stops. Cost per boost depends on the class and the current value
(table breed_costs, exported by tools/export_gamedata.py). Per-account settings: %APPDATA%/dofus_net/boost.json."""
import json, os, time
import gamedata, knowledge, store

KRC = "krc"
STATS = {10: "Force", 11: "Vitalité", 12: "Sagesse", 13: "Chance", 14: "Agilité", 15: "Intelligence"}
POINTS = 3                      # characteristic id of the unspent capital points
FIELDS_FILE = "boost_fields.json"
SETTINGS = os.path.join(os.path.dirname(store.PATH), "boost.json")


def fields():
    """characteristic id -> (krc field, verified?). Verified entries come from captures / calibration; the others take the hypothesis
    `field = id - 9`, shifted onto the free fields when a verified characteristic already uses that number."""
    ver = {int(c): int(f) for c, f in knowledge.load(FIELDS_FILE, {}).get("verified", {}).items()}
    ver.setdefault(13, 4)                                       # seen in a capture
    out = {c: (f, True) for c, f in ver.items() if c in STATS}
    free = [f for f in range(1, len(STATS) + 1) if f not in {f for f, _ in out.values()}]
    for c in sorted(STATS):
        if c in out: continue
        guess = c - 9
        if guess in free: free.remove(guess); out[c] = (guess, False)
    for c in sorted(STATS):
        if c not in out: out[c] = (free.pop(0) if free else c - 9, False)
    return out


def verify(cid, field):
    with knowledge._lock:
        d = knowledge.load(FIELDS_FILE, {}); d.setdefault("verified", {})[str(cid)] = field; knowledge.save(FIELDS_FILE, d)


def cost(breed, cid, value):
    """Capital points needed for ONE boost of characteristic `cid` whose current value is `value` (thresholds of the class)."""
    table = (gamedata.get("breed_costs", breed) or {}).get(str(cid)) or [[0, 1]]
    c = table[0][1]
    for threshold, price in table:
        if value >= threshold: c = price
    return c


def plan(points, values, breed, weights):
    """Greedy split of `points` following `weights` {cid: percent}: always buy the boost of the characteristic furthest below its share.
    -> ({cid: number of boosts}, points spent)."""
    w = {int(c): float(p) for c, p in weights.items() if int(c) in STATS and float(p) > 0}
    if not w: return {}, 0
    total = sum(w.values()); spent = {c: 0.0 for c in w}; vals = {c: int(values.get(c, 0)) for c in w}; boosts = {c: 0 for c in w}; left = points; used = 0
    for _ in range(2000):
        affordable = [c for c in w if cost(breed, c, vals[c]) <= left]
        if not affordable: break
        c = min(affordable, key=lambda c: (spent[c] / (w[c] / total), -w[c]))
        price = cost(breed, c, vals[c]); spent[c] += price; vals[c] += 1; boosts[c] += 1; left -= price; used += price
    return {c: n for c, n in boosts.items() if n}, used


def load_settings(aid):
    try:
        with open(SETTINGS, encoding="utf8") as f: d = json.load(f)
    except (OSError, ValueError): d = {}
    s = d.get(aid) or {}
    return {"auto": bool(s.get("auto", False)), "weights": {str(k): int(v) for k, v in (s.get("weights") or {"11": 100}).items()}}


def save_settings(aid, auto=None, weights=None):
    try:
        with open(SETTINGS, encoding="utf8") as f: d = json.load(f)
    except (OSError, ValueError): d = {}
    s = d.setdefault(aid, {})
    if auto is not None: s["auto"] = bool(auto)
    if weights is not None: s["weights"] = {str(int(k)): max(0, min(100, int(v))) for k, v in weights.items() if int(k) in STATS}
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, "w", encoding="utf8") as f: json.dump(d, f)
    return load_settings(aid)
