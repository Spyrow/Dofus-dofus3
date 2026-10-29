"""What the bot has learned, shared by every character: dofus_net/knowledge/*.json (versioned with the repo, no personal data).

  questbook.json  quests, steps, objectives and the dialogs / fights that solved them (client/questbook.py)
  doors.json      learned map passages: interactive elements and trigger cells (client/gamedoors.py)
Files are written atomically; ids only (quest, objective, reply, map, monster, npc), never an account or character name."""
import json, os, threading

DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "knowledge")
_lock = threading.RLock()


def path(name):
    return os.path.join(DIR, name)


def load(name, default):
    try:
        with open(path(name), encoding="utf8") as f: return json.load(f)
    except (OSError, ValueError): return default


def save(name, data):
    with _lock:
        os.makedirs(DIR, exist_ok=True)
        tmp = path(name) + ".tmp"
        with open(tmp, "w", encoding="utf8") as f: json.dump(data, f, indent=1, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path(name))
