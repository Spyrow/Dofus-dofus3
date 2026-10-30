"""LLM guide (Groq): picks dialog replies and proposes the next bot action. The model only SUGGESTS: every answer is validated against
what is really available (reply ids, actors, interactive elements, map sides, routes) before the bot does anything, and anything
unexpected is dropped -- the bot then falls back to its own logic.

Allowed actions (whitelist):
  talk {npc: actor id}            change_map {side}           use_element {element: id}
  go_map {map_id}                 attack {group: actor id}    stop {}"""
import json
import groq

SYSTEM_REPLY = ("Tu aides un bot qui joue à Dofus 3 (jeu en français). On te donne une quête, un objectif, la réplique d'un PNJ et les réponses "
                "possibles. Choisis la réponse qui fait progresser l'objectif (accepter la quête, continuer, valider), jamais celle qui refuse ou quitte "
                "sauf si c'est la seule. Réponds uniquement par un objet JSON: {\"reply_id\": <id d'une réponse proposée>, \"why\": \"<explication courte en français>\"}.")
SYSTEM_PLAN = ("Tu guides un bot qui joue à Dofus 3. À partir de l'état fourni (carte, voisines, PNJ, éléments interactifs, groupes de monstres, objectifs "
               "de quête) et éventuellement d'une instruction du joueur, propose UNE à CINQ actions parmi: "
               "talk {npc}, change_map {side}, use_element {element}, go_map {map_id}, attack {group}, stop {}. "
               "N'utilise que des identifiants présents dans l'état. Si rien de sûr n'est possible, renvoie stop. "
               "Réponds uniquement par un objet JSON: {\"actions\": [{\"action\": \"...\", ...}], \"why\": \"<explication courte en français>\"}.")
SYSTEM_HINT = ("Tu es expert de Dofus Unity (Dofus 3, la version Unity du MMORPG Ankama, en français), de ses quêtes, PNJ, zones et cartes. "
               "Un bot est bloqué sur un objectif d'une quête précise. On te donne le nom EXACT de la quête, son niveau, l'étape en cours (nom et description), "
               "l'objectif et la position actuelle du bot. Réponds avec précision pour CETTE quête de Dofus Unity (pas une autre quête au nom proche, pas Dofus 2 "
               "si c'est différent): la sous-zone et les coordonnées [x, y] de la carte où agir, et le nom exact du PNJ ou de l'élément à utiliser. "
               "N'invente rien: si tu n'es pas sûr d'une information, mets null. "
               "Réponds uniquement par un objet JSON: {\"subarea\": <nom de la sous-zone ou null>, \"x\": <entier ou null>, \"y\": <entier ou null>, "
               "\"npc\": <nom ou null>, \"why\": \"<explication courte en français>\"}.")
ACTIONS = {"talk": "npc", "change_map": "side", "use_element": "element", "go_map": "map_id", "attack": "group", "stop": None}


def available():
    return groq.configured()


def choose_reply(quest_hint, npc, text, replies):
    """-> (reply dict or None, why). Never raises: any failure means 'no opinion'."""
    if not available() or not replies: return None, None
    user = json.dumps({"quête": quest_hint, "pnj": npc, "réplique_du_pnj": (text or "")[:600],
                       "réponses": [{"id": r["id"], "texte": r["text"][:200]} for r in replies]}, ensure_ascii=False)
    try: out = groq.chat_json(SYSTEM_REPLY, user, max_tokens=200)
    except groq.GroqError: return None, None
    ids = {r["id"]: r for r in replies}
    try: rid = int(out.get("reply_id"))
    except (TypeError, ValueError): return None, None
    return (ids.get(rid), str(out.get("why") or "")[:200]) if rid in ids else (None, None)


def validate(action, state):
    """Normalise one suggested action against the real state; None if it is not allowed or not possible."""
    if not isinstance(action, dict) or action.get("action") not in ACTIONS: return None
    kind, key = action["action"], ACTIONS[action["action"]]
    if kind == "stop": return {"action": "stop"}
    try: val = int(action.get(key)) if kind != "change_map" else str(action.get(key))
    except (TypeError, ValueError): return None
    if kind == "talk" and val in {n["actor"] for n in state["npcs"]}: return {"action": kind, key: val}
    if kind == "attack" and val in {g["actor"] for g in state["groups"]}: return {"action": kind, key: val}
    if kind == "use_element" and val in {e["element"] for e in state["elements"]}: return {"action": kind, key: val}
    if kind == "change_map" and val in {s["side"] for s in state["sides"]}: return {"action": kind, key: val}
    if kind == "go_map" and val in set(state.get("reachable_maps", [])): return {"action": kind, key: val}
    return None


def plan(state, instruction=None):
    """-> (list of validated actions, why). Empty list = no usable suggestion."""
    if not available(): return [], None
    user = json.dumps({"instruction_du_joueur": instruction, "état": {k: v for k, v in state.items() if k != "reachable_maps"}}, ensure_ascii=False)
    try: out = groq.chat_json(SYSTEM_PLAN, user, max_tokens=400)
    except groq.GroqError as e: return [], f"Groq indisponible : {e}"
    raw = out.get("actions") if isinstance(out.get("actions"), list) else []
    actions = [a for a in (validate(x, state) for x in raw[:12]) if a][:5]   # validate first, then cap
    return actions, str(out.get("why") or "")[:240]


def quest_hint(quest, objective, position, level=None, step=None, step_desc=None):
    """What Groq knows about where to solve a quest objective -> ({subarea, x, y, npc, why} or {} if it knows nothing, error or None).
    Coordinates are checked later against the real maps; here they are only normalised."""
    if not available(): return {}, "clé Groq non configurée"
    user = json.dumps({"jeu": "Dofus Unity (Dofus 3)", "nom_exact_de_la_quête": quest, "niveau_de_la_quête": level, "étape_en_cours": step,
                       "description_de_l_étape": step_desc, "objectif": objective, "position_actuelle_du_bot": position}, ensure_ascii=False)
    try: out = groq.chat_json(SYSTEM_HINT, user, max_tokens=250)
    except groq.GroqError as e: return {}, str(e)
    def num(v):
        try: return int(v)
        except (TypeError, ValueError): return None
    hint = {"subarea": str(out["subarea"])[:80] if out.get("subarea") else None, "x": num(out.get("x")), "y": num(out.get("y")),
            "npc": str(out["npc"])[:80] if out.get("npc") else None, "why": str(out.get("why") or "")[:200]}
    if hint["x"] is None or hint["y"] is None: hint["x"] = hint["y"] = None
    return (hint if hint["subarea"] or hint["x"] is not None else {}), None
