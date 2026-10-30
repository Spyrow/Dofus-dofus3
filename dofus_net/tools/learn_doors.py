"""Learn map passages (doors, stairs, portals) from captures of real game sessions and merge them into doors.json.

    python tools/learn_doors.py capture1.pcapng [capture2.pcapng ...]
A passage = a client `itz {1: skill, 3: element}` followed within 5 s by a server `jow {1: new map}`; the element's cell comes from
the `jnj` of the map we left (field 7). See client/gamedoors.py."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(os.path.dirname(HERE), "client"))
import decode_pcap as d
from scan_connect import find_tool
import gamedoors, knowledge
from wire import parse, dec_varint
from collections import Counter


def split(fr):
    try:
        for f, v in parse(fr):
            if f == 3:
                a = dict(parse(v))[1]; dd = dict(parse(a)); return "S", dd[1].decode().split("/")[1], dd.get(2, b"")
            if f == 4:
                x = dict(parse(v)).get(2, b""); dd = dict(parse(x)); return "C", dd[1].decode().split("/")[1], dd.get(2, b"")
    except (ValueError, IndexError, KeyError, UnicodeDecodeError): pass
    return None, None, b""


def vi(body, num):
    return next((v for f, v in parse(body) if f == num and isinstance(v, int)), None)


def messages(pcap):
    d.TSHARK = find_tool("tshark")
    frames = []
    for sid, dirs in d.streams(pcap, [5555]).items():
        for dr, pk in dirs.items():
            for t, fr in d.frames(pk): frames.append((t, sid, fr))
    if not frames: return []
    main = Counter(s for _t, s, _f in frames).most_common(1)[0][0]
    out = [(t,) + split(fr) for t, s, fr in sorted(frames, key=lambda x: x[0]) if s == main]
    return [m for m in out if m[2]]


def last_cell(jok_body):
    """Last cell of a client `jok` path (packed varints: direction << 12 | cell). The path belongs to the map id in its field 2."""
    raw = next((v for f, v in parse(jok_body) if f == 1 and isinstance(v, bytes)), b"")
    i, last = 0, None
    while i < len(raw): v, i = dec_varint(raw, i); last = v & 0xFFF
    return last


def passages(msgs):
    found, cur, els = [], None, {}
    for i, (t, k, n, b) in enumerate(msgs):
        if k == "S" and n == "jow": cur, els = vi(b, 1), {}
        elif k == "S" and n == "jnj": els.update(gamedoors.elements(b))
        elif k == "C" and n == "itz":
            skill, elem = vi(b, 1), vi(b, 3)
            stand = next((last_cell(b2) for t2, k2, n2, b2 in reversed(msgs[:i]) if k2 == "C" and n2 == "jok" and t - t2 < 8 and vi(b2, 2) == cur), None)   # same map only
            to = next((vi(b2, 1) for t2, k2, n2, b2 in msgs[i + 1:i + 80] if k2 == "S" and n2 == "jow" and t2 - t < 5), None)
            if cur and to and to != cur: found.append((cur, skill, elem, (els.get(elem) or {}).get('cell'), to, stand))
    return found


def uses(msgs):
    """Objectives completed by using elements: a server `ibg {1: objective}` within 4 s after a client `itz` that did not change the map.
    The whole chain of such itz since the last map change is kept (a first element may open an interface that is closed with `kiq`).
    -> [(objective, map, [{element, skill, stand, kiq}, ...])]"""
    found, cur, chain = [], None, []
    for i, (t, k, n, b) in enumerate(msgs):
        if k == "S" and n == "jow": cur, chain = vi(b, 1), []
        elif k == "C" and n == "itz" and cur:
            if any(k2 == "S" and n2 == "jow" and t2 - t < 5 for t2, k2, n2, b2 in msgs[i + 1:i + 80]): chain = []; continue
            stand = next((last_cell(b2) for t2, k2, n2, b2 in reversed(msgs[:i]) if k2 == "C" and n2 == "jok" and t - t2 < 8 and vi(b2, 2) == cur), None)
            kiq = any(k2 == "C" and n2 == "kiq" and 0 < t2 - t < 6 for t2, k2, n2, b2 in msgs[i + 1:i + 60])
            chain.append({"element": vi(b, 3), "skill": vi(b, 1), "stand": stand, "kiq": kiq})
            ibg = next((vi(b2, 1) for t2, k2, n2, b2 in msgs[i + 1:i + 40] if k2 == "S" and n2 == "ibg" and t2 - t < 4), None)
            if ibg: found.append((ibg, cur, [dict(c) for c in chain])); chain = []
    return found


def steps(msgs):
    """Trigger cells: a client `jok` on the current map followed within 3 s by a server `jow` to another map, with no itz / joc in between."""
    found, cur = [], None
    for i, (t, k, n, b) in enumerate(msgs):
        if k == "S" and n == "jow": cur = vi(b, 1)
        elif k == "C" and n == "jok" and cur and vi(b, 2) == cur:
            nxt = next(((n2, b2) for t2, k2, n2, b2 in msgs[i + 1:i + 60] if t2 - t < 3 and ((k2 == "S" and n2 == "jow") or (k2 == "C" and n2 in ("itz", "joc")))), None)
            if nxt and nxt[0] == "jow" and vi(nxt[1], 1) != cur: found.append((cur, last_cell(b), vi(nxt[1], 1)))
    return found


def main():
    if len(sys.argv) < 2: sys.exit(__doc__)
    new = total = 0
    for pcap in sys.argv[1:]:
        msgs = messages(pcap)
        for oid, mp, chain in uses(msgs):
            total += 1; d = knowledge.load("element_uses.json", {})
            if d.get(str(oid)) != {"map": mp, "steps": chain}:
                with knowledge._lock: d[str(oid)] = {"map": mp, "steps": chain}; knowledge.save("element_uses.json", d)
                new += 1
            print(f"objective {oid} on map {mp}: " + " -> ".join(f"element {c['element']} from cell {c['stand']}" + (" +kiq" if c["kiq"] else "") for c in chain))
        for frm, cell, to in steps(msgs):
            total += 1; added = gamedoors.record_step(frm, cell, to); new += added
            print(f"{'+' if added else '='} map {frm} --[walk onto cell {cell}]--> map {to}")
        for frm, skill, elem, cell, to, stand in passages(msgs):
            total += 1; added = gamedoors.record(frm, skill, elem, cell, to, stand); new += added
            print(f"{'+' if added else '='} map {frm} --[element {elem}, skill {skill}, stand on cell {stand}]--> map {to}")
    print(f"{total} passage(s), {new} new -> {gamedoors.PATH}")


if __name__ == "__main__":
    main()
