"""Export id -> name tables from the installed game's data bundles (Unity 6, Addressables) to one compact JSON file.

    python tools/export_gamedata.py [--game D:\\Dofus-dofus3] [--lang fr]     (pip install UnityPy)

Data comes from <game>/Dofus_Data/StreamingAssets/Content: Data/*.asset.bundle (one table per file, MonoBehaviour with
managed references) and I18n/<lang>.bin (u8 len + lang, u32 count, count x (u32 id, u32 offset), strings = varint length +
UTF-8). Output: %APPDATA%/dofus_net/gamedata.json (outside the repo: derived from the game's files). Tables:
characteristics, spells, spell_levels, items, item_types, jobs, breeds, effects. Re-run after each game update."""
import argparse, json, os, struct, sys, warnings
warnings.filterwarnings("ignore")
import UnityPy
UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.16f1"   # UnityPlayer.dll of the analysed build
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))
import store

OUT = os.path.join(os.path.dirname(store.PATH), "gamedata.json")
GAME_DIRS = [r"D:\Dofus-dofus3", os.path.expandvars(r"%LOCALAPPDATA%\Ankama\Dofus-dofus3")]


class I18n:
    def __init__(self, path):
        self.d = open(path, "rb").read()
        n = struct.unpack_from("<I", self.d, 1 + self.d[0])[0]; base = 5 + self.d[0]
        self.idx = {k: o for k, o in (struct.unpack_from("<II", self.d, base + 8 * i) for i in range(n))}

    def __call__(self, key):
        try: o = self.idx[int(key)]
        except (KeyError, ValueError, TypeError): return None
        r = s = 0
        while True:
            c = self.d[o]; o += 1; r |= (c & 0x7F) << s; s += 7
            if not c & 0x80: break
        return self.d[o:o + r].decode("utf-8", "replace")


def table(data_dir, name):
    env = UnityPy.load(os.path.join(data_dir, f"data_assets_{name}dataroot.asset.bundle"))
    mb = next(o for o in env.objects if o.type.name == "MonoBehaviour").read_typetree()
    refs = {x["rid"]: x for x in mb["references"]["RefIds"]}
    ob = mb["objectsById"]
    return {k: refs[v["rid"]]["data"] for k, v in zip(ob["m_keys"], ob["m_values"]) if v["rid"] in refs}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--game"); ap.add_argument("--lang", default="fr")
    a = ap.parse_args()
    game = a.game or next((g for g in GAME_DIRS if os.path.isdir(g)), None) or sys.exit("game folder not found: use --game")
    content = os.path.join(game, "Dofus_Data", "StreamingAssets", "Content")
    t = I18n(os.path.join(content, "I18n", a.lang + ".bin")); dd = os.path.join(content, "Data")
    out = {"lang": a.lang}
    out["characteristics"] = {k: {"keyword": v["keyword"], "name": t(v["nameId"]), "category": v["categoryId"]} for k, v in table(dd, "characteristics").items()}
    out["characteristic_categories"] = {k: t(v["nameId"]) for k, v in table(dd, "characteristiccategories").items()}
    out["spells"] = {k: {"name": t(v["nameId"]), "desc": t(v["descriptionId"]), "levels": v["spellLevels"], "type": v["typeId"]} for k, v in table(dd, "spells").items()}
    out["spell_levels"] = {k: {"spell": v["spellId"], "grade": v["grade"], "ap": v["apCost"], "min_range": v["minRange"], "range": v["range"],
                               "cooldown": v["minCastInterval"], "max_per_turn": v["maxCastPerTurn"]} for k, v in table(dd, "spelllevels").items()}
    out["item_types"] = {k: t(v["nameId"]) for k, v in table(dd, "itemtypes").items()}
    out["items"] = {k: {"name": t(v["nameId"]), "type": v["typeId"], "level": v["level"]} for k, v in table(dd, "items").items()}
    out["interactives"] = {k: t(v["nameId"]) for k, v in table(dd, "interactives").items()}
    out["jobs"] = {k: t(v["nameId"]) for k, v in table(dd, "jobs").items()}
    out["breeds"] = {k: t(v["shortNameId"]) for k, v in table(dd, "breeds").items()}
    out["effects"] = {k: t(v["descriptionId"]) for k, v in table(dd, "effects").items()}
    # maps: id -> [x, y, name, sub area id]; scroll: id -> [right, bottom, left, top] neighbour map ids (0 = none)
    out["maps"] = {k: [v["posX"], v["posY"], t(v["nameId"]), v["subAreaId"]] for k, v in table(dd, "mapsinformation").items()}
    out["scroll"] = {k: [v["rightMapId"], v["bottomMapId"], v["leftMapId"], v["topMapId"]] for k, v in table(dd, "mapscrollactions").items()}
    out["subareas"] = {k: [t(v["nameId"]), v["areaId"]] for k, v in table(dd, "subareas").items()}
    out["areas"] = {k: [t(v["nameId"]), v["superAreaId"]] for k, v in table(dd, "areas").items()}
    sub_tab = table(dd, "subareas")
    out["subarea_monsters"] = {k: v["monsters"] for k, v in sub_tab.items() if v.get("monsters")}   # subarea id -> monster ids living there
    out["subarea_maps"] = {k: v["mapIds"] for k, v in sub_tab.items() if v.get("monsters")}
    out["superareas"] = {k: t(v["nameId"]) for k, v in table(dd, "superareas").items()}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f: json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print({k: len(v) for k, v in out.items() if isinstance(v, dict)}, "->", OUT)
    export_quests(dd, t)


def export_quests(dd, t):
    """Quests / steps / objectives / NPCs / dialog texts -> %APPDATA%/dofus_net/questdata.json (loaded lazily by gamedata.py).
    Dialog reply ids and NPC message ids are the [id, i18n id] pairs of each NPC's dialogReplies / dialogMessages."""
    npcs = table(dd, "npcs"); replies, messages = {}, {}
    for v in npcs.values():
        for x in v.get("dialogReplies", []): replies.setdefault(x["values"][0], x["values"][1])
        for x in v.get("dialogMessages", []): messages.setdefault(x["values"][0], x["values"][1])
    for k, v in table(dd, "npcmessages").items(): messages.setdefault(k, v["messageId"])
    q = {"objective_types": {k: t(v["nameId"]) for k, v in table(dd, "questobjectivetypes").items()},
         "npcs": {k: t(v["nameId"]) for k, v in npcs.items()},
         "monsters": {k: t(v["nameId"]) for k, v in table(dd, "monsters").items()},
         "replies": {k: t(i) for k, i in replies.items()}, "messages": {k: t(i) for k, i in messages.items()},
         "quests": {k: {"name": t(v["nameId"]), "steps": v["stepIds"], "level": v["levelMin"]} for k, v in table(dd, "quests").items()},
         "steps": {k: {"name": t(v["nameId"]), "desc": t(v["descriptionId"]), "objectives": v["objectiveIds"], "quest": v["questId"]}
                   for k, v in table(dd, "queststeps").items()},
         "objectives": {k: {"type": v["typeId"], "p": [v["parameters"]["parameter0"], v["parameters"]["parameter1"], v["parameters"]["parameter2"]],
                            "map": v["mapId"], "txt": t(v["parameters"]["parameter0"]) if v["typeId"] in (0, 4, 5) else None}
                       for k, v in table(dd, "questobjectives").items()}}
    path = os.path.join(os.path.dirname(OUT), "questdata.json")
    with open(path, "w", encoding="utf-8") as f: json.dump(q, f, ensure_ascii=False, separators=(",", ":"))
    print({k: len(v) for k, v in q.items()}, "->", path)


if __name__ == "__main__":
    main()
