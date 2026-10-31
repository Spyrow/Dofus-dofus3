"""Achievements ("succès"): names and points from the game's achievements bundle, and the messages that validate them.

Messages (obfuscated, build 3.7.12.12; seen when the player pressed "claim all" in a capture):
  mdp {1: [{1: achievement id, 2: character id, 3?: reward state}]}   achievements of the character, sent at login
  mdw {1: -1}                                                       claim the rewards of every achievement that has one waiting
  mdj {1: achievement id, 3: 1}                                     one achievement claimed; its reward arrives with it (krg points, iqx item, ith / lof, kaq kamas...)
Names: %APPDATA%/dofus_net/achievements.json {id: {name, points, level}}, built once from the bundle and the French texts."""
import json, os, struct, threading
import gamedata, store
from wire import parse

MDP, MDW, MDJ = "mdp", "mdw", "mdj"
PATH = os.path.join(os.path.dirname(store.PATH), "achievements.json")
_names = None
_lock = threading.Lock()


def _i18n(path):
    d = open(path, "rb").read()
    n = struct.unpack_from("<I", d, 1 + d[0])[0]; base = 5 + d[0]
    idx = {k: o for k, o in (struct.unpack_from("<II", d, base + 8 * i) for i in range(n))}
    def get(key):
        try: o = idx[int(key)]
        except (KeyError, ValueError, TypeError): return None
        r = s = 0
        while True:
            c = d[o]; o += 1; r |= (c & 0x7F) << s; s += 7
            if not c & 0x80: break
        return d[o:o + r].decode("utf-8", "replace")
    return get


def names():
    """-> {achievement id: {name, points, level}}"""
    global _names
    with _lock:
        if _names is not None: return _names
        try:
            with open(PATH, encoding="utf8") as f: _names = {int(k): v for k, v in json.load(f).items()}
            return _names
        except (OSError, ValueError): _names = {}
        import warnings; warnings.filterwarnings("ignore")
        import UnityPy, gamemaps
        UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.16f1"
        game = gamemaps._game_dir()
        if not game: return _names
        content = os.path.join(game, "Dofus_Data", "StreamingAssets", "Content")
        env = UnityPy.load(os.path.join(content, "Data", "data_assets_achievementsdataroot.asset.bundle"))
        mb = next(o for o in env.objects if o.type.name == "MonoBehaviour").read_typetree()
        txt = _i18n(os.path.join(content, "I18n", "fr.bin"))
        for r in mb["references"]["RefIds"]:
            d = r.get("data") or {}
            if "id" in d: _names[d["id"]] = {"name": txt(d.get("nameId")) or f"succès {d['id']}", "points": d.get("points", 0), "level": d.get("level", 0)}
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        with open(PATH, "w", encoding="utf8") as f: json.dump({str(k): v for k, v in _names.items()}, f, ensure_ascii=False)
        return _names


def name(achievement_id):
    return (names().get(int(achievement_id)) or {}).get("name") or f"succès {achievement_id}"


def listing(body):
    """mdp body -> [(achievement id, has a reward state)]"""
    out = []
    for f, v in parse(body):
        if f == 1 and isinstance(v, bytes):
            e = dict(parse(v))
            if isinstance(e.get(1), int): out.append((e[1], 3 in e))
    return out
