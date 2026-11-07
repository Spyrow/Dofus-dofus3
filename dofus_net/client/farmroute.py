"""Farm routes: the loop of maps to walk to farm one monster or one job resource (client/autopilot.py start_farm, web UI tabs "Trajet" and "Marché").

A route stays inside ONE zone (a sub area): the bot never detours through the neighbouring zones. Inside the zone the maps are visited in the order of a
nearest-neighbour tour from the character's map, measured in map changes (the walk graph of autopilot.distances: borders and learned doors).
  monster:  the maps of the zones where the monster lives (game data monsters.json: sub areas), outdoor maps only (interiors have a name).
  resource: the maps where an element giving the item was seen (knowledge/resource_maps.json, filled by the bot at every map it visits), per zone.
Banks: knowledge/banks.json (map ids, written when a banker NPC is met); the pods mode of the farm walks back to the nearest one.
Data files: gamedata.json and monsters.json in %APPDATA%/dofus_net (tools/export_gamedata.py, `--only-monsters` for the second one)."""
import json, os
import gamedata, gamejobs, knowledge, store

_monsters = None
_zone_maps = None


def monsters():
    """-> {monster id: {name, lv: [lowest, highest level], sub: [sub area ids], drops: [[item id, percent]]}}"""
    global _monsters
    if _monsters is None:
        try:
            with open(os.path.join(os.path.dirname(store.PATH), "monsters.json"), encoding="utf8") as f: _monsters = {int(k): v for k, v in json.load(f).items()}
        except (OSError, ValueError): _monsters = {}
    return _monsters


def _maps_by_zone():
    """{sub area id (str): [map ids]} from the maps table of the game data."""
    global _zone_maps
    if _zone_maps is None:
        _zone_maps = {}
        for mid, raw in gamedata.data().get("maps", {}).items():
            if isinstance(raw, list) and len(raw) >= 4 and raw[3]: _zone_maps.setdefault(str(raw[3]), []).append(int(mid))
    return _zone_maps


def _outdoor(mid):
    raw = gamedata.data().get("maps", {}).get(str(mid))
    return bool(raw) and not raw[2]       # interiors (taverns, houses, mines...) have a name


def _zone_of(mid):
    raw = gamedata.data().get("maps", {}).get(str(mid))
    return str(raw[3]) if isinstance(raw, list) and len(raw) >= 4 and raw[3] else None


def _zone_names(sid):
    sub = gamedata.data().get("subareas", {}).get(str(sid))
    if not sub: return None, None
    area = gamedata.data().get("areas", {}).get(str(sub[1]))
    return sub[0], (area[0] if area else None)


def _ap():
    import autopilot      # imported late: autopilot imports this module
    return autopilot


def _seen(kind, ident):
    """Maps where the target was seen: resources from the census of the bot, monsters from the fights that solved quest objectives."""
    out = set()
    if kind == "resource":
        census = knowledge.load("resource_maps.json", {})
        for skill in gamejobs.resource_skills(ident): out |= {int(m) for m in census.get(str(skill), [])}
    else:
        book = knowledge.load("questbook.json", {}).get("solved", {})
        for s in book.values():
            if s.get("kind") == "fight" and ident in (s.get("monsters") or []) and s.get("map"): out.add(int(s["map"]))
    return out


def _name(kind, ident):
    if kind == "monster": return (monsters().get(ident) or {}).get("name") or gamedata.quest_data("monsters", ident) or str(ident)
    return gamejobs.item_name(ident) or str(ident)


def _zones(kind, ident, seen):
    """[{id, name, area, maps, known}]: monsters live in the zones of the game data, resources in the zones of the maps where they were seen."""
    by = _maps_by_zone(); ids = []
    if kind == "monster": ids = [str(s) for s in (monsters().get(ident) or {}).get("sub", [])]
    else:
        for m in sorted(seen): z = _zone_of(m); ids += [z] if z and z not in ids else []
    out = []
    for sid in dict.fromkeys(ids):
        name, area = _zone_names(sid)
        maps = by.get(sid, [])
        if not name or not maps: continue
        out.append({"id": int(sid), "name": name, "area": area, "maps": sum(1 for m in maps if _outdoor(m)) if kind == "monster" else len({m for m in seen if _zone_of(m) == sid}),
                    "known": len(seen & set(maps))})
    out.sort(key=lambda z: (-z["known"], -z["maps"]))
    return out


def _tour(start, cands, allowed):
    """Nearest-neighbour order of `cands` from `start`, crossing only `allowed` maps. -> (ordered [(map, steps from the previous one)], loop length, unreachable)"""
    ap = _ap(); todo = set(cands); cur = start; order = []; total = 0
    reach = ap.distances(start, allowed)
    unreachable = {m for m in todo if m not in reach}; todo -= unreachable
    while todo:
        d = ap.distances(cur, allowed)
        nxt = min(todo, key=lambda m: (d.get(m, 10 ** 6), m))
        order.append((nxt, d.get(nxt, 0))); total += d.get(nxt, 0); todo.discard(nxt); cur = nxt
    if order: total += ap.distances(cur, allowed).get(start, 0) if start in ap.distances(cur, allowed) else 0
    return order, total, unreachable


def build(kind, ident, map_id=None, zone=None, pods=None):
    """The route of a target. kind "monster" (ident = monster id) or "resource" (ident = item id); zone = sub area id (default: the zone the character is in,
    else the nearest, else the best known); pods = free pods (gives the units a bag holds). -> dict read by the UI and by autopilot.start_farm."""
    kind = "monster" if kind == "monster" else "resource"; ident = int(ident)
    seen = _seen(kind, ident); zones = _zones(kind, ident, seen)
    res = {"kind": kind, "id": ident, "name": _name(kind, ident), "maps": [], "known": bool(seen), "zone": None, "zones": zones, "loop": 0, "skipped": 0,
           "outside": False, "to_zone": None, "weight": None, "per_trip": None, "bank": None}
    if kind == "resource":
        w = (gamedata.get("items", ident) or {}).get("weight") or 0
        res["weight"] = w or None
        if w and pods is not None: res["per_trip"] = int(pods // w)
    if not zones: return res
    ap = _ap(); reach = ap.distances(map_id) if map_id else {}
    z = next((x for x in zones if zone is not None and x["id"] == int(zone)), None)
    if z is None and map_id:      # the zone of the character, else the nearest one
        here = _zone_of(map_id); z = next((x for x in zones if str(x["id"]) == here), None)
        if z is None:
            def near(x): return min((reach.get(m, 10 ** 6) for m in _maps_by_zone().get(str(x["id"]), [])), default=10 ** 6)
            best = min(zones, key=near); z = best if near(best) < 10 ** 6 else None
    z = z or zones[0]; res["zone"] = z
    zmaps = set(_maps_by_zone().get(str(z["id"]), []))
    cands = {m for m in seen if m in zmaps} if kind == "resource" else {m for m in zmaps if _outdoor(m)} | (seen & zmaps)
    if not cands: return res
    if map_id and map_id in zmaps: start = map_id
    elif reach: start = min((m for m in cands if m in reach), key=lambda m: (reach[m], m), default=min(cands))
    else: start = min(cands)
    order, loop, unreachable = _tour(start, cands | {start}, zmaps | {start})
    maps = [(m, steps) for m, steps in order]
    res.update(loop=loop, skipped=len(unreachable), outside=bool(map_id) and map_id not in zmaps)
    if res["outside"]: res["to_zone"] = reach.get(start)
    for m, steps in maps:
        raw = gamedata.data().get("maps", {}).get(str(m)) or [0, 0, None, None]
        res["maps"].append({"id": m, "x": raw[0], "y": raw[1], "zone": z["name"], "seen": m in seen, "steps": steps})
    res["bank"] = nearest_bank(start)
    return res


def nearest_bank(map_id):
    """The known bank (a map with a banker) the fewest map changes away from `map_id`. -> {id, zone, x, y, steps} or None."""
    banks = [int(b) for b in knowledge.load("banks.json", [])]
    if not banks or not map_id: return None
    d = _ap().distances(map_id)
    ok = [b for b in banks if b in d]
    if not ok: return None
    b = min(ok, key=lambda m: (d[m], m)); raw = gamedata.data().get("maps", {}).get(str(b)) or [0, 0, None, None]
    return {"id": b, "zone": (_zone_names(raw[3])[0] if raw[3] else None) or "", "x": raw[0], "y": raw[1], "steps": d[b]}


def catalog():
    """For the Trajet tab: {"jobs": [{id, name, resources: [{item, name, level, maps}]}], "monsters": [{id, name, level: [low, high], zones: [names]}]}"""
    census = knowledge.load("resource_maps.json", {}); jobs = {}
    for sid, sk in gamejobs.skills().items():
        item = sk.get("item")
        if item is None or item < 0 or not sk.get("job"): continue
        jobs.setdefault(sk["job"], {})[item] = jobs.get(sk["job"], {}).get(item, set()) | {int(m) for m in census.get(str(sid), [])}
    out_jobs = []
    for job, items in jobs.items():
        res = [{"item": i, "name": gamejobs.item_name(i) or str(i), "level": (gamedata.get("items", i) or {}).get("level") or 0, "maps": len(maps)} for i, maps in items.items()]
        res.sort(key=lambda r: (r["level"], r["name"]))
        out_jobs.append({"id": job, "name": gamedata.get("jobs", job) or str(job), "resources": res})
    out_jobs.sort(key=lambda j: j["name"])
    by = _maps_by_zone(); mons = []
    for mid, m in monsters().items():
        zones = [n for n in (_zone_names(s)[0] for s in m.get("sub", []) if by.get(str(s))) if n]
        if zones: mons.append({"id": mid, "name": m["name"], "level": m["lv"], "zones": zones})
    mons.sort(key=lambda m: (m["level"][0], m["name"]))
    return {"jobs": out_jobs, "monsters": mons}
