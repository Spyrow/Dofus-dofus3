"""Learned map passages through interactive elements (doors, stairs, portals...).

The server decides where an element leads, so destinations are only known once a passage has been observed (capture of a real
session: tools/learn_doors.py, or one made through the UI). Stored in %APPDATA%/dofus_net/doors.json:
{from map id: [{"skill": itz.1, "element": itz.3, "cell": element cell or null, "to": map id reached}]}.
The element list of a map comes from the jnj message (field 7: {4: element model id, 5: {1: skill, 3: type}, 6: element id}); using one is
`itz {1: skill, 3: element}` -> jow."""
import json, os, shutil, threading
import knowledge, store
from wire import parse

PATH = knowledge.path("doors.json")   # shared by every character (versioned with the repo)
_OLD = os.path.join(os.path.dirname(store.PATH), "doors.json")   # earlier location (%APPDATA%): copied once
if not os.path.exists(PATH) and os.path.exists(_OLD):
    os.makedirs(knowledge.DIR, exist_ok=True); shutil.copyfile(_OLD, PATH)
_lock = threading.Lock()


def load():
    try:
        with open(PATH, encoding="utf8") as f: return {int(k): v for k, v in json.load(f).items()}
    except (OSError, ValueError): return {}


def record(frm, skill, element, cell, to, stand=None):
    """Remember that using `element` on map `frm` leads to map `to`; `stand` = the cell the player stood on to use it
    (the real client walks next to the element first). -> True if it was new."""
    if not frm or not to or frm == to: return False
    with _lock:
        d = load(); lst = d.setdefault(int(frm), [])
        for e in lst:
            if e["element"] == element:
                changed = e.get("to") != to or (cell is not None and e.get("cell") != cell) or (stand is not None and e.get("stand") != stand)
                e.update(skill=skill, cell=cell if cell is not None else e.get("cell"), to=to, stand=stand if stand is not None else e.get("stand"))
                break
        else: lst.append({"skill": skill, "element": element, "cell": cell, "to": to, "stand": stand}); changed = True
        before = len(lst); lst[:] = [e for e in lst if not (e.get("guess") and e.get("to") == to)]      # a real passage replaces the exit guessed from the arrival cell
        changed = changed or len(lst) != before
        if changed:
            os.makedirs(os.path.dirname(PATH), exist_ok=True)
            tmp = PATH + ".tmp"
            with open(tmp, "w", encoding="utf8") as f: json.dump({str(k): v for k, v in d.items()}, f, indent=1)
            os.replace(tmp, PATH)
        return changed


def record_guess(frm, cell, to):
    """Interior maps: we arrive next to the exit, so the cell we appeared on is a good first guess of the trigger cell back to `to`
    (flagged "guess" until a real passage confirms it). Nothing is added when a way back is already known. -> True if added."""
    if not frm or not to or frm == to: return False
    with _lock:
        d = load(); lst = d.setdefault(int(frm), [])
        if any(e.get("to") == to for e in lst): return False
        lst.append({"kind": "step", "guess": True, "element": None, "skill": None, "cell": cell, "to": to, "stand": None})
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf8") as f: json.dump({str(k): v for k, v in d.items()}, f, indent=1)
        os.replace(tmp, PATH)
        return True


def record_step(frm, cell, to):
    """A trigger cell: walking onto `cell` of map `frm` makes the server load map `to` (no itz / joc involved). -> True if new."""
    if not frm or not to or frm == to: return False
    with _lock:
        d = load(); lst = d.setdefault(int(frm), [])
        for e in lst:
            if e.get("kind") == "step" and e.get("cell") == cell:
                if e.get("to") == to and not e.get("guess"): return False
                e["to"] = to; e.pop("guess", None); break
        else: lst.append({"kind": "step", "element": None, "skill": None, "cell": cell, "to": to, "stand": None})
        lst[:] = [e for e in lst if not (e.get("guess") and e.get("to") == to)]      # the real trigger cell replaces any guess
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf8") as f: json.dump({str(k): v for k, v in d.items()}, f, indent=1)
        os.replace(tmp, PATH)
        return True


def elements(jnj_body):
    """jnj body -> {element id: {"skill": itz.1, "interactive": element model id (table `interactives`) or None, "type": skill type,
    "cell": None}}. The element's cell is NOT in this message (field 5.2 is not a cell)."""
    out = {}
    for f, v in parse(jnj_body):
        if f != 7 or not isinstance(v, bytes): continue
        e = dict(parse(v)); five = dict(parse(e[5])) if isinstance(e.get(5), bytes) else {}
        model = e.get(4) if isinstance(e.get(4), int) and e.get(4) < 1 << 40 else None
        if isinstance(e.get(6), int): out[e[6]] = {"skill": five.get(1), "interactive": model, "type": five.get(3), "cell": None}
    for f, v in parse(jnj_body):          # field 2: the resources of the map with their real cell {3: element id, 4: cell} (doors and workshops are not listed)
        if f == 2 and isinstance(v, bytes):
            e = dict(parse(v))
            if isinstance(e.get(3), int) and isinstance(e.get(4), int) and e[3] in out and 0 <= e[4] < 560: out[e[3]]["cell"] = e[4]
    return out


def from_map(frm):
    return load().get(int(frm), []) if frm else []
