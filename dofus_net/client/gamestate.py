"""Decode the game-state messages pushed after character selection into a web-UI snapshot (see accounts.sample_character).

Message names are obfuscated and rotate every build (re-derive with tools/scan_connect.py):
  KSC  character selection info {2: {3: character id, 4: {6: name}}}
  KRG  characteristics {1: {1: [entry]}}; entry = {2: id (absent = 0), <value>} with the value a one-field sub-message
       under field 1, 3 or 5 (empty = 0). Ids = the game's CharacteristicData ids (1 = AP, 23 = MP, 0 = hit points...).
  IRL  inventory items {2: [{1: {3: quantity, 4: item id, 5: unique id, 7: [{5: value, 10: effect id}]}, 4: position}]};
       position 63 = not equipped (Dofus 2 value). IQX = one item added later, same entry under field 2.
  IPE  jobs {1: [{2: job id, 3: level}]}
  JOW  current map {1: map id} (pushed on every map load; map ids resolve through gamedata.map_info). TENTATIVE: verified
       only against one capture (same id as the map of the actor list); confirm on a capture with a map change.
Names come from gamedata.py (tools/export_gamedata.py); without it ids are shown raw. Spells were not found in the
analysed capture (level-1 character): not decoded.
"""
import gamedata
from wire import parse

KSC, KRG, IRL, IQX, IPE, JOW = "ksc", "krg", "irl", "iqx", "ipe", "jow"
KNOWN = {0: ("Vie", "Points de vie (base)"), 1: ("Points", "PA"), 23: ("Points", "PM"),   # fallback without gamedata
         26: ("Points", "Invocations"), 48: ("Vie", "Prospection")}
NOT_EQUIPPED = 63
BASE_JOB = 1


def _signed(v):
    return v - (1 << 64) if v >= 1 << 63 else v


def _value(sub):
    """First varint found in a (possibly nested) sub-message; None if empty."""
    try: items = parse(sub)
    except (ValueError, IndexError): return None
    for _f, v in items:
        if isinstance(v, int): return _signed(v)
        if isinstance(v, bytes) and v:
            r = _value(v)
            if r is not None: return r
    return None


def characteristics(body):
    """KRG body -> {characteristic id: value} (empty entries are 0 and omitted)."""
    out = {}
    for f, inner in parse(body):
        if f != 1: continue
        for f2, entry in parse(inner):
            if f2 != 1: continue
            cid, val = 0, None
            for f3, v in parse(entry):
                if f3 == 2 and isinstance(v, int): cid = v
                elif f3 in (1, 3, 5) and isinstance(v, bytes) and val is None: val = _value(v)
            if val is not None: out[cid] = val
    return out


def selection(body):
    """KSC body -> (character id, name) or (None, None)."""
    try:
        sub = next(v for f, v in parse(body) if f == 2)
        cid = next((v for f, v in parse(sub) if f == 3 and isinstance(v, int)), None)
        info = next((v for f, v in parse(sub) if f == 4), b"")
        name = next((v.decode("utf-8", "replace") for f, v in parse(info) if f == 6 and isinstance(v, bytes)), None)
        return cid, name
    except (StopIteration, ValueError, IndexError):
        return None, None


def _item(entry):
    """One inventory entry -> {gid, uid, qty, position, effects: [(effect id, value)]}."""
    pos, sub = None, b""
    for f, v in parse(entry):
        if f == 1 and isinstance(v, bytes): sub = v
        elif f == 4 and isinstance(v, int): pos = v
    it = {"gid": None, "uid": None, "qty": 1, "position": pos, "effects": []}
    for f, v in parse(sub):
        if f == 4 and isinstance(v, int): it["gid"] = v
        elif f == 5 and isinstance(v, int): it["uid"] = v
        elif f == 3 and isinstance(v, int): it["qty"] = v
        elif f == 7 and isinstance(v, bytes):
            e = {f2: v2 for f2, v2 in parse(v)}
            it["effects"].append((e.get(10), _signed(e[5]) if isinstance(e.get(5), int) else None))
    return it


def inventory_items(body, single=False):
    """IRL body (single=False) or IQX body (single=True) -> list of raw items."""
    out = []
    for f, v in parse(body):
        if f == 2 and isinstance(v, bytes):
            out.append(_item(v))
    return out


def jobs(body):
    out = []
    for f, v in parse(body):
        if f != 1 or not isinstance(v, bytes): continue
        e = {f2: v2 for f2, v2 in parse(v)}
        if isinstance(e.get(2), int): out.append((e[2], e.get(3, 1)))
    return out


def _item_view(it):
    meta = gamedata.get("items", it["gid"])
    stats = []
    for eid, val in it["effects"]:
        txt = gamedata.effect_text(eid, val) if val is not None else gamedata.get("effects", eid)
        if txt: stats.append(txt)
    return {"name": meta["name"] if meta else f"Objet #{it['gid']}",
            "type": (gamedata.get("item_types", meta["type"]) if meta else None) or "",
            "level": meta["level"] if meta else None, "qty": it["qty"],
            "equipped": it["position"] not in (None, NOT_EQUIPPED), "stats": stats}


def _characteristic_groups(stats):
    cats = {1: "Vie"}
    groups = {}
    for k, v in sorted(stats.items(), key=lambda kv: (gamedata.get("characteristics", kv[0]) or {}).get("category", 99)):
        meta = gamedata.get("characteristics", k)
        if meta and meta["name"]:
            c = meta["category"]
            g = cats.get(c) or gamedata.get("characteristic_categories", c) or "Autres"
            label = meta["name"]
        else:
            g, label = KNOWN.get(k, ("Autres (id brut)", f"#{k}"))
        group = groups.setdefault(g, {})
        group[label if label not in group else f"{label} (#{k})"] = str(v)
    return groups


def snapshot(messages, host=None):
    """messages: iterable of (type name, body). The LAST krg wins (the server re-sends it after entering the map)."""
    cid = name = stats = job_list = None
    items = {}; map_ids = []
    for t, body in messages:
        try:
            if t == KSC: cid, name = selection(body)
            elif t == KRG: stats = characteristics(body)
            elif t in (IRL, IQX):
                for it in inventory_items(body): items[it["uid"] if it["uid"] is not None else len(items)] = it
            elif t == IPE: job_list = jobs(body)
            elif t == JOW:
                mid = next((v for f, v in parse(body) if f == 1 and isinstance(v, int)), None)
                if mid and (not map_ids or map_ids[-1] != mid): map_ids.append(mid)
        except (ValueError, IndexError):
            continue
    snap = {"character": {}}
    if name: snap["character"]["name"] = name
    if cid: snap["character"]["id"] = cid
    if host and host.startswith("dofus2-ga-"): snap["character"]["server"] = host.split("-")[2].split(".")[0].capitalize()
    if stats: snap["characteristics"] = _characteristic_groups(stats)
    if items: snap["inventory"] = {"items": [_item_view(it) for it in items.values()]}
    pos = gamedata.map_info(map_ids[-1]) if map_ids else None
    if pos:
        snap["position"] = pos
        pos["exits"] = {d: {k: i[k] for k in ("x", "y", "map_id", "subarea") } for d, mid in gamedata.neighbours(pos["map_id"]).items()
                        if (i := gamedata.map_info(mid))}
        snap["maps"] = gamedata.area_maps(pos["map_id"])
        snap["trail"] = [{"x": m["x"], "y": m["y"]} for m in map(gamedata.map_info, map_ids[:-1]) if m]
    elif map_ids: snap["position"] = {"map_id": map_ids[-1]}
    if job_list:
        snap["jobs"] = [{"name": gamedata.get("jobs", j) or f"Métier #{j}", "level": lvl} for j, lvl in job_list if j != BASE_JOB]
    return snap
