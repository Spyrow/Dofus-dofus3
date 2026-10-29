"""Quest and dialog messages (names are obfuscated and rotate every build: see login.py constants).

  ICA  quests at login {5: [{1: 1, 2: quest id}] completed, 6: [quest state] active}
  ICJ  quest state {1: {1: quest id, 3: {1: step id, 3: [{1: 1 = objective in progress (absent = done), 4: objective id}]}}}
  IBG  objective done {1: objective id, 2: quest id}        ICB  quest changed {1: quest id}
  ILX  dialog opened {3: map id, 5: actor id}   ILF  dialog step {1: [{1: reply id}], 3: NPC message id}   KIS  dialog closed
  An NPC actor (negative id) carries its template id at 3.1.4.7 of its map entry. Texts: gamedata.quest_data (questdata.json).
"""
import gamedata
from wire import parse


def _sub(body, *path):
    """Follow nested length-delimited fields, e.g. _sub(b, 3, 1, 4) -> bytes or None."""
    try:
        for num in path:
            body = next(v for f, v in parse(body) if f == num and isinstance(v, bytes))
        return body
    except (StopIteration, ValueError, IndexError, TypeError):
        return None


def _int(body, num):
    try: return next((v for f, v in parse(body) if f == num and isinstance(v, int)), None)
    except (ValueError, IndexError): return None


def npc_template(entry):
    """Map actor entry -> NPC template id (3.1.4.7) or None."""
    sub = _sub(entry, 3, 1, 4)
    return _int(sub, 7) if sub is not None else None


def quest_state(body):
    """ICJ body or one ICA 'active' entry -> (quest id, step id, {objective id: in progress}) or None."""
    q = _sub(body, 1) if _int(body, 1) is None else body
    if q is None: return None
    qid = _int(q, 1); st = _sub(q, 3)
    if qid is None or st is None: return None
    objs = {}
    for f, v in parse(st):
        if f == 3 and isinstance(v, bytes): objs[_int(v, 4)] = bool(_int(v, 1))
    return qid, _int(st, 1), objs


def active_quests(ica_body):
    out = []
    for f, v in parse(ica_body):
        if f == 6 and isinstance(v, bytes):
            q = quest_state(v)
            if q: out.append(q)
    return out


def dialog_step(body):
    """ILF body -> (npc message id, [reply ids])."""
    replies = []
    for f, v in parse(body):
        if f == 1 and isinstance(v, bytes):
            r = _int(v, 1)
            if r is not None: replies.append(r)
    return _int(body, 3), replies


def objective_text(oid):
    """Human text of a quest objective, e.g. 'Aller voir Caporale Mynerve'."""
    o = gamedata.quest_data("objectives", oid)
    if not o: return f"objectif {oid}"
    label = gamedata.quest_data("objective_types", o["type"]) or f"type {o['type']}"
    p = o["p"]
    npc = lambda i: gamedata.quest_data("npcs", i) or f"PNJ {i}"
    mon = lambda i: gamedata.quest_data("monsters", i) or f"monstre {i}"
    item = lambda i: (gamedata.get("items", i) or {}).get("name") or f"objet {i}"
    txt = o.get("txt") or p[0]
    args = {0: (txt,), 4: (txt,), 5: (txt,), 1: (npc(p[0]),), 2: (npc(p[0]), item(p[1]), p[2]), 3: (npc(p[0]), item(p[1]), p[2]), 9: (npc(p[0]),),
            10: (npc(p[0]),), 6: (mon(p[0]), p[1]), 7: (mon(p[0]),), 14: (mon(p[0]), p[1]), 13: (mon(p[0]),), 16: (mon(p[0]), p[1], p[2]),
            8: (item(p[0]),), 17: (item(p[0]), p[1])}.get(o["type"], (p[0], p[1], p[2]))
    for i, a in enumerate(args, 1): label = label.replace(f"#{i}", str(a))
    return label


def quest_view(qid, step_id, objs):
    q = gamedata.quest_data("quests", qid) or {}
    st = gamedata.quest_data("steps", step_id) or {}
    out = []
    for oid, active in objs.items():
        o = gamedata.quest_data("objectives", oid) or {}
        mp = gamedata.map_info(o["map"]) if o.get("map") else None
        out.append({"id": oid, "text": objective_text(oid), "active": active,
                    "where": f"{mp['subarea']} [{mp['x']}, {mp['y']}]" if mp else None})
    return {"id": qid, "name": q.get("name") or f"Quête {qid}", "step": st.get("name"), "desc": st.get("desc"), "objectives": out}
