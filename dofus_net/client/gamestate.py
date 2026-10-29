"""Decode the game-state messages pushed after character selection into a web-UI snapshot (see accounts.sample_character).

Message names are obfuscated and rotate every build (re-derive with tools/scan_connect.py): KSC = character selection
info {2: {3: character id, 4: {6: name}}}, KRG = characteristics {1: {1: [entry]}}.
A characteristic entry is {2: id (absent = 0), <value>} where the value is a one-field sub-message under field 1, 3 or 5
(empty = 0). Names for the ids below are INFERRED from the values of a level-1 character (AP 6, MP 3, prospecting 100,
55 HP = 50 + 5 x level) and from Dofus 2's ids; every other id is shown raw ("#<id>") rather than guessed.
"""
from wire import parse

KSC, KRG = "ksc", "krg"
KNOWN = {0: ("Vie", "Points de vie (base)"), 1: ("Points", "PA"), 23: ("Points", "PM"),
         26: ("Points", "Invocations"), 48: ("Vie", "Prospection")}


def _signed(v):
    return v - (1 << 64) if v >= 1 << 63 else v


def _value(sub):
    """First varint found in a (possibly nested) sub-message; None if empty."""
    try: items = parse(sub)
    except (ValueError, IndexError): return None
    for _f, v in items:
        if isinstance(v, int): return _signed(v)
        if isinstance(v, bytes) and v:
            r = _value(v)
            if r is not None: return r
    return None


def characteristics(body):
    """KRG body -> {characteristic id: value} (empty entries are 0 and omitted)."""
    out = {}
    for f, inner in parse(body):
        if f != 1: continue
        for f2, entry in parse(inner):
            if f2 != 1: continue
            cid, val = 0, None
            for f3, v in parse(entry):
                if f3 == 2 and isinstance(v, int): cid = v
                elif f3 in (1, 3, 5) and isinstance(v, bytes) and val is None: val = _value(v)
            if val is not None: out[cid] = val
    return out


def selection(body):
    """KSC body -> (character id, name) or (None, None)."""
    try:
        sub = next(v for f, v in parse(body) if f == 2)
        cid = next((v for f, v in parse(sub) if f == 3 and isinstance(v, int)), None)
        info = next((v for f, v in parse(sub) if f == 4), b"")
        name = next((v.decode("utf-8", "replace") for f, v in parse(info) if f == 6 and isinstance(v, bytes)), None)
        return cid, name
    except (StopIteration, ValueError, IndexError):
        return None, None


def snapshot(messages, host=None):
    """messages: iterable of (type name, body). The LAST krg wins (the server re-sends it after entering the map)."""
    cid = name = stats = None
    for t, body in messages:
        if t == KSC: cid, name = selection(body)
        elif t == KRG:
            try: stats = characteristics(body)
            except (ValueError, IndexError): pass
    snap = {"character": {}}
    if name: snap["character"]["name"] = name
    if cid: snap["character"]["id"] = cid
    if host and host.startswith("dofus2-ga-"): snap["character"]["server"] = host.split("-")[2].split(".")[0].capitalize()
    if stats:
        groups = {}
        for k, v in sorted(stats.items()):
            g, label = KNOWN.get(k, ("Autres (id brut)", f"#{k}"))
            groups.setdefault(g, {})[label] = str(v)
        snap["characteristics"] = {g: groups[g] for g in ("Vie", "Points", "Autres (id brut)") if g in groups}
    return snap
