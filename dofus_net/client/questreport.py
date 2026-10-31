"""Which quests of an area (Incarnam...) are finished, active or still to do.

Finished / active quests come from the server (ica at login: {5: [{2: quest id}] finished, 6: active}). The quests of an area are those whose objectives
happen on a map of that area (questdata.json), plus the ones whose scraped guide (knowledge/dpln_quests.json, dofuspourlesnoobs.com) starts on a
place of that area. For the ones not done, the guide gives where to start, the level and the prerequisites."""
import re
import dpln, gamedata, gamejobs, knowledge

_guides = None


def _guide(name):
    """Scraped guide by quest name; the 6.5 MB file is read once."""
    global _guides
    if _guides is None: _guides = knowledge.load(dpln.FILE, {}).get("quests", {})
    return _guides.get(dpln._norm(name))


def _area_places(area):
    """{(subarea name, x, y)} of the maps of the area."""
    out = set()
    for mid in gamedata.data().get("maps", {}):
        i = gamedata.map_info(int(mid))
        if i and i["area"] == area and i["subarea"]: out.add((gamejobs.norm(i["subarea"]), i["x"], i["y"]))
    return out


def area_quests(area="Incarnam"):
    """-> {quest id: {name, level, why}}"""
    import json, os, store
    with open(os.path.join(os.path.dirname(store.PATH), "questdata.json"), encoding="utf8") as f: qd = json.load(f)
    places = _area_places(area); out = {}
    for qid, q in qd["quests"].items():
        maps = []
        for sid in q.get("steps") or []:
            for oid in (qd["steps"].get(str(sid)) or {}).get("objectives", []):
                m = (qd["objectives"].get(str(oid)) or {}).get("map")
                if m: maps.append(int(m))
        areas = [(gamedata.map_info(m) or {}).get("area") for m in maps]
        why = None
        if areas and sum(a == area for a in areas) * 2 > len(areas): why = "objectifs"
        else:
            g = _guide(q.get("name"))
            m = re.match(r"\s*(.+?)\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]", (g or {}).get("start") or "")
            if m and (gamejobs.norm(m[1]), int(m[2]), int(m[3])) in places and not any(a and a != area for a in areas): why = "guide"
        if why: out[int(qid)] = {"name": q["name"], "level": q.get("level"), "why": why}
    return out


_npc_maps = None


def npc_maps():
    """{npc template id: {map ids}} where the quests of the game send the player to see that NPC (objectives "Aller voir / Montrer à / Retourner voir #1")."""
    global _npc_maps
    if _npc_maps is None:
        gamedata.quest_data("npcs", 0)
        qd = gamedata._quest or {}; out = {}
        for ob in qd.get("objectives", {}).values():
            if ob.get("type") in (1, 2, 3, 9) and ob.get("map") and ob.get("p"): out.setdefault(int(ob["p"][0]), set()).add(int(ob["map"]))
        _npc_maps = out
    return _npc_maps


def starter(quest_name):
    """The NPC that starts a quest, read in the scraped guide: the known NPC (one that has a map) named first in its first steps.
    -> (npc name, [template ids], [map ids]) or None."""
    g = _guide(quest_name)
    if not g: return None
    text = gamejobs.norm(" ".join((g.get("steps") or [])[:2])); best = None
    gamedata.quest_data("npcs", 0)
    for nid, nm in (gamedata._quest or {}).get("npcs", {}).items():
        n = gamejobs.norm(nm)
        if len(n) < 4 or int(nid) not in npc_maps(): continue
        m = re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", text)
        if m and (best is None or m.start() < best[0]): best = (m.start(), nm)
    if not best: return None
    ids = [int(i) for i in gamedata.npc_ids_named(best[1])]
    maps = sorted({m for i in ids for m in npc_maps().get(i, ())})
    return (best[1], ids, maps) if maps else None


def npcs_in_text(text):
    """Known NPCs (they have a map) named in a text, in the order they appear. -> [(npc name, [template ids], [map ids])]"""
    t = gamejobs.norm(text); gamedata.quest_data("npcs", 0); found = {}
    for nid, nm in (gamedata._quest or {}).get("npcs", {}).items():
        n = gamejobs.norm(nm)
        if len(n) < 4 or int(nid) not in npc_maps(): continue
        m = re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", t)
        if m and (nm not in found or m.start() < found[nm]): found[nm] = m.start()
    out = []
    for nm, _pos in sorted(found.items(), key=lambda kv: kv[1]):
        ids = [int(x) for x in gamedata.npc_ids_named(nm)]
        maps = sorted({m for x in ids for m in npc_maps().get(x, ())})
        if maps: out.append((nm, ids, maps))
    return out


def guide_npcs(quest_name, max_steps=10):
    """The NPCs a quest's scraped guide sends the player to, in the order it names them: known NPCs (they have a map) found in its first steps.
    -> [(npc name, [template ids], [map ids])]"""
    g = _guide(quest_name)
    if not g: return []
    text = gamejobs.norm(" ".join((g.get("steps") or [])[:max_steps]))
    gamedata.quest_data("npcs", 0)
    found = {}
    for nid, nm in (gamedata._quest or {}).get("npcs", {}).items():
        n = gamejobs.norm(nm)
        if len(n) < 5 or int(nid) not in npc_maps(): continue
        m = re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", text)
        if m and nm not in found or (m and m.start() < found[nm][0]): found[nm] = (m.start(), nm)
    out = []
    for _pos, nm in sorted(found.values()):
        ids = [int(x) for x in gamedata.npc_ids_named(nm)]
        maps = sorted({m for x in ids for m in npc_maps().get(x, ())})
        if maps: out.append((nm, ids, maps))
    return out


def quoted_replies(quest_name):
    """Texts between quotes in the scraped guide («Commander une bière»): the dialog replies the guide tells to choose. -> set of normalised strings."""
    g = _guide(quest_name); out = set()
    for step in (g or {}).get("steps") or []:
        for q in re.findall(r"[«“\"]\s*([^»”\"]{6,140}?)\s*[»”\"]", step):
            n = gamejobs.squash(q)
            if len(n) >= 6: out.add(n)
    return out


def seller(item_name, quest_name):
    """The NPC the scraped guide of a quest says sells `item_name` ("Grobid vous vendra du Lailait pour 4 kamas"): the known NPC named in the sentence
    that mentions the item with "acheter / vendre", or in the sentence before / after it. -> (npc name, [template ids], [map ids]) or None."""
    g = _guide(quest_name)
    item = gamejobs.norm(item_name)
    if not g or not item: return None
    steps = [gamejobs.norm(s) for s in (g.get("steps") or [])]
    gamedata.quest_data("npcs", 0)
    for i, sn in enumerate(steps):
        if not gamejobs.mentions(item, sn) or not re.search(r"achet|vend", sn): continue
        text = " ".join(steps[max(0, i - 1):i + 2]); best = None
        for nid, nm in (gamedata._quest or {}).get("npcs", {}).items():
            n = gamejobs.norm(nm)
            if len(n) < 4 or int(nid) not in npc_maps(): continue
            m = re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", text)
            if m and (best is None or m.start() < best[0]): best = (m.start(), nm)
        if best:
            ids = [int(x) for x in gamedata.npc_ids_named(best[1])]
            maps = sorted({m for x in ids for m in npc_maps().get(x, ())})
            if maps: return best[1], ids, maps
    return None


def report(finished, active, area="Incarnam"):
    """-> {"finished": [...], "active": [...], "todo": [...]}, each item {id, name, level, start, prereq, url}."""
    res = {"finished": [], "active": [], "todo": [], "ignored": []}
    skip = knowledge.load("quests_ignored.json", {})      # seasonal / mis-classified quests: {quest id: reason}
    for qid, q in sorted(area_quests(area).items(), key=lambda kv: (kv[1]["level"] or 0, kv[0])):
        if str(qid) in skip and qid not in finished and qid not in active:
            res["ignored"].append({"id": qid, "name": q["name"], "level": q["level"], "reason": skip[str(qid)]}); continue
        g = _guide(q["name"]) or {}
        item = {"id": qid, "name": q["name"], "level": q["level"], "start": g.get("start"), "prereq": g.get("prereq"), "url": g.get("url"), "guide": bool(g), "how": q["why"]}
        res["finished" if qid in finished else "active" if qid in active else "todo"].append(item)
    return res
