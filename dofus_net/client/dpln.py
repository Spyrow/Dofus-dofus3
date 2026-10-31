"""Quest walkthroughs scraped from dofuspourlesnoobs.com (dofus_net/knowledge/dpln_quests.json, built by tools/scrape_dpln.py)."""
import re, unicodedata
import knowledge

FILE = "dpln_quests.json"


def _norm(v):
    s = unicodedata.normalize("NFKD", str(v or "")).casefold().replace("œ", "oe").replace("æ", "ae")
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in s if not unicodedata.combining(c))).strip()


def find(quest_name):
    """Scraped guide of a quest by its (exact, accent/case-insensitive) name, or None."""
    return knowledge.load(FILE, {}).get("quests", {}).get(_norm(quest_name))


def walkthrough(quest_name, limit=3500):
    """Plain text of the guide (start place + steps), cut to `limit` chars for an LLM prompt; None if the quest is unknown."""
    q = find(quest_name)
    if not q: return None
    head = f"Position de lancement : {q['start']}\n" if q.get("start") else ""
    return (head + "\n".join(q.get("steps") or []))[:limit]
