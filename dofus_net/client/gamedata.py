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


def get(table, key):
    return data().get(table, {}).get(str(key))


def effect_text(effect_id, value):
    """'#1{{~1~2 à }}#2 Vitalité' + 5 -> '5 Vitalité' (second parameter dropped)."""
    d = get("effects", effect_id)
    if not d: return None
    d = re.sub(r"\{\{[^}]*\}\}", "", d).replace("#1", str(value)).replace("#2", "")
    return re.sub(r"\s+", " ", d).strip()
