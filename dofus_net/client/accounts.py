"""Multi-account store for the web UI: %APPDATA%/dofus_net/accounts.json (same folder as store.py, OUTSIDE the repo).

Character snapshots (keys: character, characteristics, equipment, inventory, jobs, spells) live in %APPDATA%/dofus_net/characters/<account id>.json. Nothing in this repo fills them yet
(the game protocol is only decoded up to the handshake, see login.py): whatever later decodes the game stream should
write that file, schema in sample_character() below.
"""
import json, os, secrets, threading, time
import store
from haapi import refresh_api_key

DIR = os.path.dirname(store.PATH)
PATH = os.path.join(DIR, "accounts.json")
CHAR_DIR = os.path.join(DIR, "characters")
_lock = threading.RLock()


def _read():
    try:
        with open(PATH, encoding="utf8") as f: return json.load(f)
    except (OSError, ValueError): return []


def _write(accts):
    os.makedirs(DIR, exist_ok=True)
    with open(PATH, "w", encoding="utf8") as f: json.dump(accts, f, indent=1)


def _migrate(accts):
    """Import the single account saved by connect.py (tokens.json) the first time."""
    legacy = store.load()
    if not accts and legacy:
        accts = [{"id": secrets.token_hex(4), "label": "Compte 1", "added": time.time(), "refreshed": None,
                  "key": legacy["key"], "refresh_token": legacy["refresh_token"]}]
        _write(accts)
    return accts


def public(a):
    snap = char_path(a["id"])
    return {"id": a["id"], "label": a["label"], "added": a["added"], "refreshed": a.get("refreshed"),
            "snapshot": os.path.getmtime(snap) if os.path.exists(snap) else None}   # never expose key/refresh_token


def list_accounts():
    with _lock: return [public(a) for a in _migrate(_read())]


def get(aid):
    with _lock: return next((a for a in _migrate(_read()) if a["id"] == aid), None)


def add(label, key, refresh_token):
    with _lock:
        accts = _migrate(_read())
        a = {"id": secrets.token_hex(4), "label": label or f"Compte {len(accts) + 1}", "added": time.time(),
             "refreshed": time.time(), "key": key, "refresh_token": refresh_token}
        _write(accts + [a]); return public(a)


def rename(aid, label):
    with _lock:
        accts = _read(); a = next((x for x in accts if x["id"] == aid), None)
        if not a: return None
        a["label"] = label; _write(accts); return public(a)


def remove(aid):
    with _lock:
        accts = _read(); keep = [x for x in accts if x["id"] != aid]
        if len(keep) == len(accts): return False
        _write(keep)
        try: os.remove(char_path(aid))
        except OSError: pass
        return True


def refresh(aid):
    """RefreshApiKey rotates the refresh token: persist the new one. Raises SystemExit(msg) on HTTP errors."""
    with _lock:
        accts = _read(); a = next((x for x in accts if x["id"] == aid), None)
        if not a: return None
        r = refresh_api_key(a["key"], a["refresh_token"])
        a.update(key=r["key"], refresh_token=r["refresh_token"], refreshed=time.time())
        _write(accts); return public(a)


def char_path(aid):
    return os.path.join(CHAR_DIR, "".join(c for c in aid if c.isalnum()) + ".json")


def load_character(aid):
    try:
        with open(char_path(aid), encoding="utf8") as f: return json.load(f)
    except (OSError, ValueError): return None


def sample_character():
    """Schema of a snapshot (all keys optional) + the data shown for the built-in demo account."""
    return {
        "character": {"name": "Démo-Cra", "level": 200, "breed": "Cra", "sex": "F", "server": "Draconiros",
                      "guild": "Les Lapins", "kamas": 1234567, "xp_pct": 42},
        "characteristics": {
            "Vie": {"Points de vie": "3 120", "Initiative": "1 450", "Prospection": "135"},
            "Points": {"PA": "12", "PM": "6", "Portée": "+4", "Invocations": "2"},
            "Élémentaires": {"Force": "120", "Intelligence": "80", "Chance": "950", "Agilité": "320"},
            "Dommages": {"Dommages": "+85", "% Dommages": "+40", "Critique": "+22", "Soins": "+0"},
            "Résistances": {"% Terre": "20", "% Feu": "15", "% Eau": "25", "% Air": "30"}},
        "equipment": [
            {"slot": "Coiffe", "name": "Coiffe d'exemple", "level": 195, "stats": ["+400 Vitalité", "+8 Chance"]},
            {"slot": "Cape", "name": "Cape d'exemple", "level": 190, "stats": ["+300 Vitalité", "+5% Résistance Air"]},
            {"slot": "Arme", "name": "Arc d'exemple", "level": 200, "stats": ["Dégâts Eau 40-50", "+1 PO"]},
            {"slot": "Anneau 1", "name": None}, {"slot": "Anneau 2", "name": None},
            {"slot": "Amulette", "name": "Amulette d'exemple", "level": 198, "stats": ["+1 PA", "+60 Initiative"]}],
        "inventory": {"capacity": 5000, "items": [
            {"name": "Blé", "type": "Ressource", "level": 1, "qty": 342},
            {"name": "Peau de Bouftou", "type": "Ressource", "level": 10, "qty": 58},
            {"name": "Potion de rappel", "type": "Consommable", "level": 1, "qty": 12},
            {"name": "Épée d'exemple", "type": "Arme", "level": 120, "qty": 1}]},
        "jobs": [{"name": "Paysan", "level": 100, "xp_pct": 35}, {"name": "Boulanger", "level": 87, "xp_pct": 60},
                 {"name": "Bûcheron", "level": 12, "xp_pct": 5}],
        "spells": [
            {"name": "Flèche magique", "level": 6, "ap": 3, "range": "1-12", "cooldown": 0, "element": "Feu",
             "desc": "Inflige des dégâts de feu à distance."},
            {"name": "Flèche de dispersion", "level": 6, "ap": 3, "range": "1-10", "cooldown": 2, "element": "Eau",
             "desc": "Inflige des dégâts d'eau et retire des PM."},
            {"name": "Tir critique", "level": 5, "ap": 4, "range": "1-9", "cooldown": 1, "element": "Air", "desc": ""}]}
