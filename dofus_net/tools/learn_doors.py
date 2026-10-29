"""Learn map passages (doors, stairs, portals) from captures of real game sessions and merge them into doors.json.

    python tools/learn_doors.py capture1.pcapng [capture2.pcapng ...]
A passage = a client `itz {1: skill, 3: element}` followed within 5 s by a server `jow {1: new map}`; the element's cell comes from
the `jnj` of the map we left (field 7). See client/gamedoors.py."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(os.path.dirname(HERE), "client"))
import decode_pcap as d
from scan_connect import find_tool
import gamedoors
from wire import parse
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


def passages(msgs):
    found, cur, els = [], None, {}
    for i, (t, k, n, b) in enumerate(msgs):
        if k == "S" and n == "jow": cur, els = vi(b, 1), {}
        elif k == "S" and n == "jnj": els.update(gamedoors.elements(b))
        elif k == "C" and n == "itz":
            skill, elem = vi(b, 1), vi(b, 3)
            to = next((vi(b2, 1) for t2, k2, n2, b2 in msgs[i + 1:i + 80] if k2 == "S" and n2 == "jow" and t2 - t < 5), None)
            if cur and to and to != cur: found.append((cur, skill, elem, (els.get(elem) or {}).get('cell'), to))
    return found


def main():
    if len(sys.argv) < 2: sys.exit(__doc__)
    new = total = 0
    for pcap in sys.argv[1:]:
        for frm, skill, elem, cell, to in passages(messages(pcap)):
            total += 1; added = gamedoors.record(frm, skill, elem, cell, to); new += added
            print(f"{'+' if added else '='} map {frm} --[element {elem}, skill {skill}, cell {cell}]--> map {to}")
    print(f"{total} passage(s), {new} new -> {gamedoors.PATH}")


if __name__ == "__main__":
    main()
