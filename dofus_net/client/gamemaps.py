"""Map cells, pathfinding and movement keys.

Map data: Content/Map/Data/mapdata_assets_world_*.bundle, one MonoBehaviour `map_<id>` with 560 cells (14 wide x 20 tall, rows
interleaved) and the neighbour map ids. `mov` = walkable; `mapChangeData` = bit mask of the map exits a cell leads to
(1 E, 2 SE, 4 S, 8 SW, 16 W, 32 NW, 64 N, 128 NE; screen right/bottom/left/top = E/S/W/N, or on some maps the two diagonal bits framing the side). tools/index_maps.py builds
%APPDATA%/dofus_net/mapindex.json (map id -> bundle). Parsed maps are cached in %APPDATA%/dofus_net/mapcells/<id>.json.

Cell geometry (Dofus 2 MapPoint, checked against the movement paths of a capture): in "grid" coordinates cell i has
p = i // 28, r = i % 28, c = r % 14 -> x = p + c + (r >= 14), y = c - p. Directions 0..7 (E, SE, S, SW, W, NW, N, NE) are the
steps (+1,+1) (+1,0) (+1,-1) (0,-1) (-1,-1) (-1,0) (-1,+1) (0,+1). A movement key is (direction << 12) | cell."""
import heapq, json, os
import store

WIDTH, HEIGHT, CELLS = 14, 20, 560
DIRS = [(1, 1), (1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1), (0, 1)]
EXIT_BIT = {"right": 1, "bottom": 4, "left": 16, "top": 64}
DIR_DIAG_BITS = {"right": 1 | 2 | 128, "bottom": 4 | 2 | 8, "left": 16 | 8 | 32, "top": 64 | 32 | 128}   # edge corners count for both sides
STEP_SECONDS = 0.25      # observed ~0.11-0.14 s per cell on foot; slower is safer (the server checks the confirmation delay)

DATA_DIR = os.path.dirname(store.PATH)
INDEX_PATH = os.path.join(DATA_DIR, "mapindex.json")
CACHE_DIR = os.path.join(DATA_DIR, "mapcells")
GAME_DIRS = [r"D:\Dofus-dofus3", os.path.expandvars(r"%LOCALAPPDATA%\Ankama\Dofus-dofus3")]

_index = None
_cache = {}


def to_xy(cell):
    p, r = divmod(cell, 28)
    c = r % WIDTH
    return p + c + (1 if r >= WIDTH else 0), c - p


_FROM_XY = {to_xy(i): i for i in range(CELLS)}


def from_xy(x, y):
    return _FROM_XY.get((x, y))


def direction(a, b):
    """Direction (0..7) of the single step a -> b (cells), or None if they are not adjacent."""
    (ax, ay), (bx, by) = to_xy(a), to_xy(b)
    try: return DIRS.index((bx - ax, by - ay))
    except ValueError: return None


def _game_dir():
    return next((g for g in GAME_DIRS if os.path.isdir(g)), None)


def load(map_id):
    """-> {id, neighbours: {top, bottom, left, right}, cells: [[mov, los, change], ...560]} or None (unknown map / no index)."""
    global _index
    map_id = int(map_id)
    if map_id in _cache: return _cache[map_id]
    path = os.path.join(CACHE_DIR, f"{map_id}.json")
    try:
        with open(path, encoding="utf8") as f: m = json.load(f)
        if m and len(m["cells"][0]) < 6: raise ValueError("cache without zones")      # cached before linkedZone / moveZone / altitude were read
    except (OSError, ValueError, KeyError, IndexError):
        m = _read_bundle(map_id)
        if m:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(path, "w", encoding="utf8") as f: json.dump(m, f, separators=(",", ":"))
    if len(_cache) > 64: _cache.clear()
    _cache[map_id] = m
    return m


def element_positions(map_id):
    """-> {interactive element id: (x, y)} in map pixels (read from the bundle, cached next to the cell data); {} when unknown."""
    map_id = int(map_id); path = os.path.join(CACHE_DIR, f"{map_id}.elements.json")
    try:
        with open(path, encoding="utf8") as f: return {int(k): tuple(v) for k, v in json.load(f).items()}
    except (OSError, ValueError): pass
    global _index
    if _index is None:
        try:
            with open(INDEX_PATH, encoding="utf8") as f: _index = json.load(f)
        except (OSError, ValueError): _index = {}
    bundle, game = (_index or {}).get(str(map_id)), _game_dir()
    if not bundle or not game: return {}
    import warnings; warnings.filterwarnings("ignore")
    import UnityPy
    UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.16f1"
    env = UnityPy.load(os.path.join(game, "Dofus_Data", "StreamingAssets", "Content", "Map", "Data", bundle))
    out = {}
    for o in env.objects:
        if o.type.name != "MonoBehaviour" or o.peek_name() != f"map_{map_id}": continue
        for r in o.read_typetree()["references"]["RefIds"]:
            d = r.get("data") or {}; e = d.get("<interactiveId>k__BackingField")
            if e and "position" in d: out[e] = (d["position"]["x"], d["position"]["y"])
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf8") as f: json.dump({str(k): v for k, v in out.items()}, f)
    return out


def element_cells(map_id):
    """-> {interactive element id: cell} for the elements the bundle gives a cellId (resources: confirmed against the itw message of a
    harvest; workshops and doors have none); {} when unknown. Cached next to the cell data."""
    map_id = int(map_id); path = os.path.join(CACHE_DIR, f"{map_id}.cells.json")
    try:
        with open(path, encoding="utf8") as f: return {int(k): v for k, v in json.load(f).items()}
    except (OSError, ValueError): pass
    global _index
    if _index is None:
        try:
            with open(INDEX_PATH, encoding="utf8") as f: _index = json.load(f)
        except (OSError, ValueError): _index = {}
    bundle, game = (_index or {}).get(str(map_id)), _game_dir()
    if not bundle or not game: return {}
    import warnings; warnings.filterwarnings("ignore")
    import UnityPy
    UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.16f1"
    env = UnityPy.load(os.path.join(game, "Dofus_Data", "StreamingAssets", "Content", "Map", "Data", bundle))
    out = {}
    for o in env.objects:
        if o.type.name != "MonoBehaviour" or o.peek_name() != f"map_{map_id}": continue
        for r in o.read_typetree()["references"]["RefIds"]:
            d = r.get("data") or {}; e = d.get("<interactiveId>k__BackingField"); c = d.get("cellId")
            if e and isinstance(c, int) and c >= 0: out[e] = c
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf8") as f: json.dump({str(k): v for k, v in out.items()}, f)
    return out


def cell_center(cell):
    """Pixel centre of a cell (cells are 86 x 43 px diamonds, rows 21.5 px apart)."""
    r, k = divmod(cell, 14)
    return (k * 2 + (r % 2) + 1) * 43.0, (r + 1) * 21.5


def stand_candidates(m, pos, limit=8, reach=150):
    """Walkable cells most likely to be the use-cell of an element at pixel `pos` (static elements have no cell in the map data). Skills are used
    from a direct neighbour (distance 1) of the element's cell; that cell is estimated at (x + 615.5, 565 - y) px (fitted on 19 uses of the real
    client, so only a hint: ~13 of them have the real use-cell among the first 3 candidates). Direct neighbours of the estimated cell come first,
    then the nearest cells by pixel distance."""
    tx, ty = pos[0] + 615.5, 565.25 - pos[1]
    cells = [(c, (cell_center(c)[0] - tx) ** 2 + (cell_center(c)[1] - ty) ** 2) for c in range(CELLS) if walkable(m, c)]
    if not cells: return []
    est = min(cells, key=lambda t: t[1])[0]; ex, ey = to_xy(est)
    def direct(c): x, y = to_xy(c); return abs(x - ex) + abs(y - ey) == 1
    return [c for c, d in sorted(cells, key=lambda t: (not direct(t[0]), t[1])) if d ** 0.5 <= reach + 120 or direct(c)][:limit]


def _read_bundle(map_id):
    global _index
    if _index is None:
        try:
            with open(INDEX_PATH, encoding="utf8") as f: _index = json.load(f)
        except (OSError, ValueError): _index = {}
    bundle, game = _index.get(str(map_id)), _game_dir()
    if not bundle or not game: return None
    import warnings; warnings.filterwarnings("ignore")
    import UnityPy
    UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.16f1"
    env = UnityPy.load(os.path.join(game, "Dofus_Data", "StreamingAssets", "Content", "Map", "Data", bundle))
    for o in env.objects:
        if o.type.name != "MonoBehaviour" or o.peek_name() != f"map_{map_id}": continue
        t = o.read_typetree()
        return {"id": map_id,
                "neighbours": {d: t[k] for d, k in (("top", "topNeighbourId"), ("bottom", "bottomNeighbourId"),
                                                    ("left", "leftNeighbourId"), ("right", "rightNeighbourId"))},
                "cells": [[c["mov"], c["los"], c["mapChangeData"], c.get("linkedZone", 0), c.get("moveZone", 0), c.get("altitude", 0)] for c in sorted(t["cellsData"], key=lambda c: c["cellNumber"])]}
    return None


def walkable(m, cell):
    return 0 <= cell < CELLS and bool(m["cells"][cell][0])


# A border can be marked by its middle bit alone (1 right, 4 bottom, 16 left, 64 top) or, on some maps, by the two diagonal bits that frame it
# (right 2+128, bottom 2+8, left 8+32, top 32+128; e.g. flag 10 = SE+SW along a bottom edge, 130 = SE+NE along a right edge).
DIAG_BITS = {"right": (2, 128), "bottom": (2, 8), "left": (8, 32), "top": (32, 128)}


def exit_cells(m, side):
    """Walkable cells whose mapChangeData leads to `side` (right/bottom/left/top). A lone diagonal bit (a corner cell) is ambiguous: ignored."""
    mid, (d1, d2) = EXIT_BIT[side], DIAG_BITS[side]
    return [i for i, c in enumerate(m["cells"]) if c[0] and (c[2] & mid or (c[2] & d1 and c[2] & d2))]


def can_step(m, a, b):
    """Is the step a -> b (adjacent cells) allowed? A map can hold sunken / raised areas (altitude, linkedZone) joined by a ramp (moveZone = 1 cells):
    the altitude only changes along the ramp. In the real client's paths (about 330 steps over two captures) every step that changes altitude
    has both cells on the ramp; the steps the server refuses are the ones that leave a ramp sideways (204 -> 203 on map 154010374, 329 -> 328
    on map 154010113)."""
    ca, cb = m["cells"][a], m["cells"][b]
    if len(ca) < 6 or len(cb) < 6: return True
    return ca[5] == cb[5] or bool(ca[4] and cb[4])


def find_path(m, start, goal, blocked=(), diagonals_only=False):
    """Shortest 8-direction path start -> goal over walkable cells (uniform step cost, small penalty per turn so that paths
    have few corners). diagonals_only=True restricts the steps to the 4 odd directions (1, 3, 5, 7): the only moves allowed in a FIGHT
    (the server answers `jpc` with code -7 to a straight horizontal / vertical step).
    -> list of cells including both ends, or None."""
    if not (walkable(m, start) and walkable(m, goal)): return None
    if start == goal: return [start]
    blocked = set(blocked); gx, gy = to_xy(goal)
    dirs = [(i, d) for i, d in enumerate(DIRS) if not diagonals_only or i % 2]
    def h(c): x, y = to_xy(c); return abs(x - gx) + abs(y - gy) if diagonals_only else max(abs(x - gx), abs(y - gy))
    heap = [(h(start), 0.0, start, -1)]; best = {(start, -1): 0.0}; prev = {}
    while heap:
        _f, g, cell, d = heapq.heappop(heap)
        if cell == goal:
            path = [cell]; k = (cell, d)
            while k in prev: k = prev[k]; path.append(k[0])
            return path[::-1]
        if g > best.get((cell, d), 1e18): continue
        x, y = to_xy(cell)
        for nd, (dx, dy) in dirs:
            n = from_xy(x + dx, y + dy)
            if n is None or not walkable(m, n) or (n in blocked and n != goal): continue
            if not can_step(m, cell, n): continue
            ng = g + 1 + (0.01 if d not in (-1, nd) else 0)
            if ng < best.get((n, nd), 1e18):
                best[(n, nd)] = ng; prev[(n, nd)] = (cell, d)
                heapq.heappush(heap, (ng + h(n), ng, n, nd))
    return None


def movement_keys(path):
    """Cell path -> the waypoints the client sends in `jok`: one key (direction << 12 | cell) at the start and at every change
    of direction, plus the end cell (carrying the direction of the last segment)."""
    if len(path) < 2: return []
    dirs = [direction(a, b) for a, b in zip(path, path[1:])]
    keys = [(dirs[0] << 12) | path[0]]
    for i in range(1, len(dirs)):
        if dirs[i] != dirs[i - 1]: keys.append((dirs[i] << 12) | path[i])
    keys.append((dirs[-1] << 12) | path[-1])
    return keys


def expand_keys(keys):
    """Inverse of movement_keys (cells only); used to check a path decoded from the network."""
    cells = [keys[0] & 0xFFF]
    for k, nxt in zip(keys, keys[1:]):
        d, cur, end = k >> 12, cells[-1], nxt & 0xFFF
        while cur != end:
            x, y = to_xy(cur); cur = from_xy(x + DIRS[d][0], y + DIRS[d][1])
            if cur is None: return cells
            cells.append(cur)
    return cells


def duration(path):
    return STEP_SECONDS * max(0, len(path) - 1) + 0.4
