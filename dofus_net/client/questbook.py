"""Quest book: every quest the bot has seen, and how its objectives were solved (dofus_net/knowledge/questbook.json).

  quests[qid]    {name, seen, steps: {step id: {name, objectives: {oid: text}}}}
  solved[oid]    {qid, kind: "dialog"|"fight", npc, map, path: [reply ids], monsters, count, last}   (what worked)
  dialogs["npc@map"]  [{path: [reply ids], completed: [oid], active: [oid], count}]               (every dialog outcome, good or dead end)
suggest_reply() turns the dialogs into the next reply to give: follow a path that already solved one of the active objectives,
otherwise avoid the continuations known to end without progress. Ids only: usable by any character."""
import time
import knowledge

FILE = "questbook.json"
KEEP_PATHS = 12   # outcomes remembered per npc@map


def _book():
    b = knowledge.load(FILE, {})
    b.setdefault("version", 1); b.setdefault("quests", {}); b.setdefault("solved", {}); b.setdefault("dialogs", {})
    return b


def observe(qid, step_id, objectives, name=None, step_name=None, texts=None):
    """A quest state was seen (login list or update): remember the quest, its step and objective texts."""
    with knowledge._lock:
        b = _book(); q = b["quests"].setdefault(str(qid), {"name": name, "seen": 0, "steps": {}})
        q["name"] = name or q.get("name"); q["seen"] = q.get("seen", 0) + 1
        st = q["steps"].setdefault(str(step_id), {"name": step_name, "objectives": {}})
        st["name"] = step_name or st.get("name")
        for oid in objectives: st["objectives"][str(oid)] = (texts or {}).get(oid) or st["objectives"].get(str(oid))
        knowledge.save(FILE, b)


def get_hint(oid):
    """Groq hint remembered for an objective -> {subarea, x, y, npc, why}; {} when Groq had nothing; None when never asked."""
    return _book().setdefault("hints", {}).get(str(oid))


def record_hint(oid, qid, hint):
    with knowledge._lock:
        b = _book(); b.setdefault("hints", {})[str(oid)] = dict(hint, qid=qid, last=int(time.time()))
        knowledge.save(FILE, b)


def record_dialog(npc, map_id, path, completed, active):
    """A dialog ended. completed = [(qid, oid)] validated within a few seconds; active = objective ids that were open."""
    if not npc or not path: return
    done = sorted({o for _q, o in completed})
    with knowledge._lock:
        b = _book(); key = f"{npc}@{map_id}"; lst = b["dialogs"].setdefault(key, [])
        entry = next((e for e in lst if e["path"] == list(path) and e["completed"] == done), None)
        if entry: entry["count"] += 1
        else: lst.append({"path": list(path), "completed": done, "active": sorted(active), "count": 1}); del lst[:-KEEP_PATHS]
        for qid, oid in completed:
            s = b["solved"].setdefault(str(oid), {"qid": qid, "count": 0})
            s.update(kind="dialog", npc=npc, map=map_id, path=list(path), last=int(time.time())); s["count"] += 1
        knowledge.save(FILE, b)


def record_fight(map_id, monsters, completed):
    if not completed: return
    with knowledge._lock:
        b = _book()
        for qid, oid in completed:
            s = b["solved"].setdefault(str(oid), {"qid": qid, "count": 0})
            s.update(kind="fight", map=map_id, monsters=sorted(set(monsters)), last=int(time.time())); s["count"] += 1
        knowledge.save(FILE, b)


def suggest_reply(npc, map_id, prefix, replies, active, refuse=None):
    """Next reply (one of `replies`, dicts with id/text) or None to let the caller decide.
    1. a recorded path starting with `prefix` that solved an active objective -> its next reply;
    2. otherwise the first non-refusing reply that is not a known dead end after `prefix`."""
    if not npc or not replies: return None
    ids = {r["id"]: r for r in replies}; n = len(prefix); active = set(active)
    entries = _book()["dialogs"].get(f"{npc}@{map_id}", [])
    for e in sorted(entries, key=lambda e: -e["count"]):
        if set(e["completed"]) & active and e["path"][:n] == list(prefix) and len(e["path"]) > n and e["path"][n] in ids:
            return ids[e["path"][n]]
    dead = {e["path"][n] for e in entries if not e["completed"] and e["path"][:n] == list(prefix) and len(e["path"]) > n}
    fresh = [r for r in replies if r["id"] not in dead]
    if len(fresh) < len(replies):   # some continuation is known to lead nowhere: choose among the others
        ok = [r for r in fresh if not (refuse and refuse.search(r["text"]))]
        return (ok or fresh or replies)[0]
    return None


def summary():
    b = _book()
    return {"quests": len(b["quests"]), "solved": len(b["solved"]), "dialogs": sum(len(v) for v in b["dialogs"].values()),
            "list": [{"id": int(k), "name": v.get("name"), "steps": len(v["steps"]),
                      "solved": sum(1 for st in v["steps"].values() for o in st["objectives"] if o in b["solved"])}
                     for k, v in sorted(b["quests"].items(), key=lambda kv: int(kv[0]))]}
