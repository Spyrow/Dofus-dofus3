"""Id -> name lookups from %APPDATA%/dofus_net/gamedata.json (written by tools/export_gamedata.py from the installed game).
Every lookup returns None when the file or the id is missing: callers fall back to the raw id."""
import json, os, re
import store

PATH = os.path.join(os.path.dirname(store.PATH), "gamedata.json")
_data = None


def data():
    global _data
    if _data is None:
        try:
            with open(PATH, encoding="utf8") as f: _data = json.load(f)
        except (OSError, ValueError): _data = {}
    return _data


_quest = None


def quest_data(table, key):
    """Lookup in questdata.json (quests, steps, objectives, npcs, monsters, replies, messages, objective_types)."""
    global _quest
    if _quest is None:
        try:
            with open(os.path.join(os.path.dirname(PATH), "questdata.json"), encoding="utf8") as f: _quest = json.load(f)
        except (OSError, ValueError): _quest = {}
    return _quest.get(table, {}).get(str(key))


def npc_ids_named(name):
    """Ids of the NPC templates with this exact name (a boss is often both a monster and an NPC)."""
    quest_data("npcs", 0)
    return [int(i) for i, n in (_quest or {}).get("npcs", {}).items() if n == name]


def get(table, key):
    return data().get(table, {}).get(str(key))


def map_info(map_id):
    """-> {x, y, map_id, name, subarea, area, world} or None for an unknown map / missing gamedata."""
    m = get("maps", map_id)
    if not m: return None
    sub = get("subareas", m[3]) or [None, None]
    area = get("areas", sub[1]) or [None, None]
    return {"x": m[0], "y": m[1], "map_id": int(map_id), "name": m[2], "subarea": sub[0], "area": area[0],
            "world": get("superareas", area[1])}


def neighbours(map_id):
    """-> {'right'|'bottom'|'left'|'top': neighbour map id} (existing edges only)."""
    s = get("scroll", map_id) or [0, 0, 0, 0]
    return {d: i for d, i in zip(("right", "bottom", "left", "top"), s) if i}


def area_maps(map_id, limit=600):
    """Coordinates of the maps in the same area as map_id (what the UI map tab draws as 'known' maps)."""
    m = get("maps", map_id)
    if not m: return []
    area = (get("subareas", m[3]) or [None, None])[1]
    out = []
    for k, v in data().get("maps", {}).items():
        sub = get("subareas", v[3])
        if sub and sub[1] == area:
            out.append({"x": v[0], "y": v[1], "name": v[2], "area": (get("areas", area) or [None])[0]})
            if len(out) >= limit: break
    return out


def monster_maps(monster_id):
    """Maps of every subarea where `monster_id` lives (the monster can appear on any of them)."""
    out = []
    for sid, mons in data().get("subarea_monsters", {}).items():
        if int(monster_id) in mons: out += data().get("subarea_maps", {}).get(sid, [])
    return out


def effect_text(effect_id, value):
    """'#1{{~1~2 à }}#2 Vitalité' + 5 -> '5 Vitalité' (second parameter dropped)."""
    d = get("effects", effect_id)
    if not d: return None
    d = re.sub(r"\{\{[^}]*\}\}", "", d).replace("#1", str(value)).replace("#2", "")
    return re.sub(r"\s+", " ", d).strip()
