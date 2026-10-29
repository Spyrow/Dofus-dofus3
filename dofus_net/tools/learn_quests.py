"""Fill the quest book (dofus_net/knowledge/questbook.json) from captures of real game sessions.

    python tools/learn_quests.py capture1.pcapng [capture2.pcapng ...]
Replays each capture: quest states (ica / icj), dialogs (ilt, ilx, ilf, iky, kis) and the objectives the server validated within
6 s after a dialog (ibg). Every dialog outcome is stored, good or dead end, so the autopilot can reuse what worked."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(os.path.dirname(HERE), "client"))
import learn_doors as L
import gamedata, gamequests, questbook
from wire import parse

WINDOW = 6.0


def s64(v):
    return None if v is None else (v - (1 << 64) if v >= 1 << 63 else v)


def replay(msgs):
    cur, npcs, quests, dlg, pending = None, {}, {}, None, None
    finished, done = [], []
    for t, k, n, b in msgs:
        if k == "S":
            if n == "jow": cur, npcs = L.vi(b, 1), {}
            elif n in ("jnj", "jns"):
                for f, v in parse(b):
                    if f == (3 if n == "jnj" else 1) and isinstance(v, bytes):
                        e = dict(parse(v)); aid = s64(e.get(1)) if isinstance(e.get(1), int) else None
                        if aid is not None and aid < 0:
                            tpl = gamequests.npc_template(v)
                            if tpl: npcs[aid] = tpl
            elif n == "ica":
                quests = {q: (st, objs) for q, st, objs in gamequests.active_quests(b)}
                for q, (st, objs) in quests.items(): observe(q, st, objs)
            elif n == "icj":
                q = gamequests.quest_state(b)
                if q: quests[q[0]] = (q[1], q[2]); observe(*q)
            elif n == "ibg": done.append((t, L.vi(b, 2), L.vi(b, 1)))
            elif n == "ilx":
                actor = s64(L.vi(b, 5)) if pending is None else pending
                dlg = {"npc": npcs.get(actor), "map": cur, "path": [], "t": t,
                       "active": [o for _q, (_s, objs) in quests.items() for o, a in objs.items() if a]}
            elif n == "kis" and dlg: finished.append((dlg, t)); dlg = None
        elif k == "C":
            if n == "ilt": pending = s64(L.vi(b, 3))
            elif n == "iky" and dlg is not None: dlg["path"].append(L.vi(b, 1))
    return finished, done


def observe(qid, step, objs):
    q = gamedata.quest_data("quests", qid) or {}; st = gamedata.quest_data("steps", step) or {}
    questbook.observe(qid, step, list(objs), q.get("name"), st.get("name"), {o: gamequests.objective_text(o) for o in objs})


def main():
    if len(sys.argv) < 2: sys.exit(__doc__)
    total = good = 0
    for pcap in sys.argv[1:]:
        finished, done = replay(L.messages(pcap))
        for dlg, t_end in finished:
            completed = [(q, o) for t, q, o in done if dlg["t"] <= t <= t_end + WINDOW]
            questbook.record_dialog(dlg["npc"], dlg["map"], dlg["path"], completed, dlg["active"])
            total += 1; good += bool(completed)
            npc = gamedata.quest_data("npcs", dlg["npc"]) if dlg["npc"] else None
            print(f"{'+' if completed else '·'} dialogue avec {npc or dlg['npc']} (carte {dlg['map']}) : {len(dlg['path'])} réponse(s)"
                  + (f" -> objectif(s) validé(s) : {', '.join(gamequests.objective_text(o) for _q, o in completed)}" if completed else " -> sans effet"))
    s = questbook.summary(); print(f"{total} dialogue(s) ({good} utiles) ; livre : {s['quests']} quêtes, {s['solved']} objectifs résolus, {s['dialogs']} dialogues")


if __name__ == "__main__":
    main()
