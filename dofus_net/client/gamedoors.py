"""Learned map passages through interactive elements (doors, stairs, portals...).

The server decides where an element leads, so destinations are only known once a passage has been observed (capture of a real
session: tools/learn_doors.py, or one made through the UI). Stored in %APPDATA%/dofus_net/doors.json:
{from map id: [{"skill": itz.1, "element": itz.3, "cell": element cell or null, "to": map id reached}]}.
The element list of a map comes from the jnj message (field 7: {4: cell, 5: {1: skill, 3: type}, 6: element id}); using one is
`itz {1: skill, 3: element}` -> jow."""
import json, os, threading
import store
from wire import parse

PATH = os.path.join(os.path.dirname(store.PATH), "doors.json")
_lock = threading.Lock()


def load():
    try:
        with open(PATH, encoding="utf8") as f: return {int(k): v for k, v in json.load(f).items()}
    except (OSError, ValueError): return {}


def record(frm, skill, element, cell, to):
    """Remember that using `element` on map `frm` leads to map `to`. -> True if it was new."""
    if not frm or not to or frm == to: return False
    with _lock:
        d = load(); lst = d.setdefault(int(frm), [])
        for e in lst:
            if e["element"] == element:
                changed = e.get("to") != to or (cell is not None and e.get("cell") != cell)
                e.update(skill=skill, cell=cell if cell is not None else e.get("cell"), to=to)
                break
        else: lst.append({"skill": skill, "element": element, "cell": cell, "to": to}); changed = True
        if changed:
            os.makedirs(os.path.dirname(PATH), exist_ok=True)
            tmp = PATH + ".tmp"
            with open(tmp, "w", encoding="utf8") as f: json.dump({str(k): v for k, v in d.items()}, f, indent=1)
            os.replace(tmp, PATH)
        return changed


def elements(jnj_body):
    """jnj body -> {element id: {"skill": itz.1, "cell": cell or None, "type": skill type}}"""
    out = {}
    for f, v in parse(jnj_body):
        if f != 7 or not isinstance(v, bytes): continue
        e = dict(parse(v)); five = dict(parse(e[5])) if isinstance(e.get(5), bytes) else {}
        cell = e.get(4) if isinstance(e.get(4), int) and e.get(4) < 560 else None
        if isinstance(e.get(6), int): out[e[6]] = {"skill": five.get(1), "cell": cell, "type": five.get(3)}
    return out


def from_map(frm):
    return load().get(int(frm), []) if frm else []
