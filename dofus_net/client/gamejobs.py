"""Jobs: recipes, harvest / craft messages and the experience of each job.

Messages (obfuscated, build 3.7.12.12; observed in a capture of a real harvest and craft, see GAME_PROTOCOL.md):
  harvest  C itz {1: skill, 3: element} -> S itw {1: {3: element, 4: element cell, 5: 2 = being harvested}}, itx, itv {1: element, 5: duration}
           ~3 s later S itt {1: element} (end), isz {2: quantity, 3: item id} (loot), irx {3: {1: new stack quantity, 3: stack uid}},
           ipz (job experience), itw {.. 5: 1}. An element that is not available answers nothing.
  craft    C itz on a workshop -> S jyz {1: recipe skill id}, irl.  C kar {1: item to craft} (the server puts the ingredients on the table:
           S kch {3: {3: quantity, 4: item id, 5: uid}}).  C kba {1: 1, 2: 1} -> S kco {2: {4: item}} (new item), irx, ipz, ipa.
           C kiq (twice) closes the interface -> S kbr.
  ipz      job experience {1: {1: source (2 harvest, 3 craft), 2: experience, 3: experience where the level started,
           5: job id, 6: experience of the next level}}.
Recipes come from the game's recipes bundle (UnityPy), cached in %APPDATA%/dofus_net/recipes.json."""
import json, os, re, threading, unicodedata
import gamedata, knowledge, store
from wire import parse

IPZ, ISZ, ITT, ITW, ITX, ITV, JYZ, KCH, KCO, KBR, IRX = "ipz", "isz", "itt", "itw", "itx", "itv", "jyz", "kch", "kco", "kbr", "irx"
KAR, KBA, KIQ = "kar", "kba", "kiq"
ITO, IQN = "ito", "iqn"   # equip / move an item {1: 1, 2: item uid, 3: position} (1 = weapon slot, 63 = not worn) / the server confirms {1: uid, 3: position}
MESSAGES = {IPZ, ISZ, ITT, ITW, ITV, JYZ, KCH, KCO, KBR, IRX, IQN, "mdj", "mdp"}
RECIPES_PATH = os.path.join(os.path.dirname(store.PATH), "recipes.json")
STANDS_FILE = "workshop_stands.json"    # {map id: {element id: cell}}: where the character stood when the workshop opened
_recipes = None
_lock = threading.Lock()


def norm(text):
    return "".join(c for c in unicodedata.normalize("NFKD", str(text or "")).casefold().replace("œ", "oe").replace("æ", "ae") if not unicodedata.combining(c))


def recipes():
    """-> {result item id: {job, skill, level, ingredients: [(item id, quantity)]}}"""
    global _recipes
    with _lock:
        if _recipes is not None: return _recipes
        try:
            with open(RECIPES_PATH, encoding="utf8") as f: _recipes = {int(k): v for k, v in json.load(f).items()}
        except (OSError, ValueError):
            _recipes = _export()
        return _recipes


def _export():
    import warnings; warnings.filterwarnings("ignore")
    import UnityPy, gamemaps
    UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.16f1"
    game = gamemaps._game_dir()
    if not game: return {}
    env = UnityPy.load(os.path.join(game, "Dofus_Data", "StreamingAssets", "Content", "Data", "data_assets_recipesdataroot.asset.bundle"))
    mb = next(o for o in env.objects if o.type.name == "MonoBehaviour").read_typetree()
    out = {}
    for x in mb["references"]["RefIds"]:
        d = x.get("data") or {}
        if x.get("type", {}).get("class") == "RecipeData" and "resultId" in d:
            out[d["resultId"]] = {"job": d["jobId"], "skill": d["skillId"], "level": d["resultLevel"],
                                  "ingredients": list(zip(d["ingredientIds"], d["quantities"]))}
    os.makedirs(os.path.dirname(RECIPES_PATH), exist_ok=True)
    with open(RECIPES_PATH, "w", encoding="utf8") as f: json.dump({str(k): v for k, v in out.items()}, f)
    return out


SKILLS_PATH = os.path.join(os.path.dirname(store.PATH), "skills.json")
_skills = None


def skills():
    """-> {skill id: {job, item (resource it gives or -1), range, craftable: [item ids]}} from the game's skills bundle. The `type` of a jnj element is
    its skill id: a resource element gives `item`, a workshop element crafts `craftable`, `range` is the distance it can be used from."""
    global _skills
    with _lock:
        if _skills is not None: return _skills
        try:
            with open(SKILLS_PATH, encoding="utf8") as f: _skills = {int(k): v for k, v in json.load(f).items()}
            if _skills and "action" not in next(iter(_skills.values())): raise ValueError("cache without the element action")
        except (OSError, ValueError):
            import warnings; warnings.filterwarnings("ignore")
            import UnityPy, gamemaps
            UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.16f1"
            game = gamemaps._game_dir(); _skills = {}
            if game:
                env = UnityPy.load(os.path.join(game, "Dofus_Data", "StreamingAssets", "Content", "Data", "data_assets_skillsdataroot.asset.bundle"))
                mb = next(o for o in env.objects if o.type.name == "MonoBehaviour").read_typetree()
                for r in mb["references"]["RefIds"]:
                    d = r.get("data") or {}
                    if "id" in d: _skills[d["id"]] = {"job": d.get("parentJobId"), "item": d.get("gatheredRessourceItem", -1), "range": d.get("range", 1), "action": d.get("elementActionId"),
                                                       "craftable": list(d.get("craftableItemIds") or [])}
                os.makedirs(os.path.dirname(SKILLS_PATH), exist_ok=True)
                with open(SKILLS_PATH, "w", encoding="utf8") as f: json.dump({str(k): v for k, v in _skills.items()}, f)
        return _skills


def door_types():
    """Element types (= skill ids) whose action is "use a passage" (elementActionId 16: 184, 310, 339...): doors, stairs, exits."""
    return {k for k, v in skills().items() if v.get("action") == 16}


def resource_skills(item_id):
    """Skill ids (= jnj element types) whose elements give `item_id`."""
    return {k for k, v in skills().items() if v["item"] == item_id}


def skill_range(skill_id):
    return (skills().get(skill_id) or {}).get("range", 1)


def hunting_knives():
    """Item ids of the hunter's knives ("Couteau de chasse", "Couteau de chasse de Berb Nhin"...): equipped, a killed monster leaves meat."""
    return {int(k) for k, v in gamedata.data().get("items", {}).items() if isinstance(v, dict) and "couteau de chasse" in norm(v.get("name"))}


def squash(text):
    """norm() with every run of punctuation (apostrophes of any kind, quotes, dots) turned into one space: texts of the game and of the guides compare equal."""
    return re.sub(r"[^a-z0-9]+", " ", norm(text)).strip()


def mentions(item_norm, text_norm):
    """Does a normalised text name the item, singular or plural ("Bave de Bouftou" / "Baves de Bouftou")?"""
    pat = r"(?<![a-z0-9])" + r"\s+".join(re.escape(w) + "s?" for w in item_norm.split()) + r"(?![a-z0-9])"
    return re.search(pat, text_norm) is not None


def recipe(result_id):
    return recipes().get(int(result_id))


def item_name(item_id):
    return (gamedata.get("items", item_id) or {}).get("name")


def resource_models(item_id):
    """Interactive element models (table `interactives`) that give item `item_id`: same name as the item (Blé, Ortie, Goujon...)."""
    name = norm(item_name(item_id))
    if not name: return set()
    return {int(k) for k, v in gamedata.data().get("interactives", {}).items() if isinstance(v, str) and norm(v) == name}


def workshop_models(job_id):
    """Interactive element models of the workshops of a job ("Atelier des paysans", "Four"...)."""
    job = norm(gamedata.get("jobs", job_id))
    out = set()
    for k, v in gamedata.data().get("interactives", {}).items():
        if not isinstance(v, str): continue
        n = norm(v)
        if job and job in n and re.match(r"(atelier|etabli|four|pressoir)", n): out.add(int(k))
    return out


# ---- stands ----
def stand(map_id, element):
    return (knowledge.load(STANDS_FILE, {}).get(str(map_id)) or {}).get(str(element))


def remember_stand(map_id, element, cell):
    with knowledge._lock:
        d = knowledge.load(STANDS_FILE, {}); m = d.setdefault(str(map_id), {})
        if m.get(str(element)) != cell: m[str(element)] = cell; knowledge.save(STANDS_FILE, d)


# ---- decoders ----
def _int(body, num):
    return next((v for f, v in parse(body) if f == num and isinstance(v, int)), None)


def _sub(body, num):
    return next((v for f, v in parse(body) if f == num and isinstance(v, bytes)), b"")


def loot(body):
    """isz -> (item id, quantity)"""
    return _int(body, 3), _int(body, 2)


def job_xp(body):
    """ipz -> {source, xp, floor, next, job} or None"""
    e = _sub(body, 1)
    job = _int(e, 5)
    if job is None: return None
    return {"source": _int(e, 1), "xp": _int(e, 2) or 0, "floor": _int(e, 3) or 0, "next": _int(e, 6), "job": job}


def stack_quantity(body):
    """irx -> (stack uid, new quantity) or None"""
    e = _sub(body, 3)
    uid, qty = _int(e, 3), _int(e, 1)
    return (uid, qty) if uid is not None and qty is not None else None


def ingredient(body):
    """kch -> (item id, quantity) put on the craft table"""
    e = _sub(_sub(body, 3), 1)
    return _int(e, 4), _int(e, 3)


def crafted(body):
    """kco -> the new item entry ({gid, uid, qty, ...} like an inventory item) or None"""
    import gamestate
    e = _sub(_sub(body, 2), 4)
    if not e: return None
    return gamestate._item(parse_wrap(e))


def parse_wrap(item_body):
    """An item body (3: quantity, 4: item id, 5: uid) in the shape gamestate._item expects (item = field 1 of the entry)."""
    from wire import f_bytes
    return f_bytes(1, item_body)


def level_of(floor, known=1):
    """Job level from the experience where it started (20 / 60 / 120 / 200... = 10 n (n + 1) for level n + 1); `known` when it does not fit."""
    if floor <= 0: return 1
    n = 1
    while 10 * n * (n + 1) < floor: n += 1
    return n + 1 if 10 * n * (n + 1) == floor else known
