"""Index which Unity bundle holds each map: writes %APPDATA%/dofus_net/mapindex.json {map id: bundle file name}.

    python tools/index_maps.py [--game D:\Dofus-dofus3]     (pip install UnityPy; a few minutes, run once per game update)
Each Content/Map/Data/mapdata_assets_world_*.bundle holds one or more MonoBehaviour named `map_<id>`."""
import argparse, json, os, re, sys, time, warnings
warnings.filterwarnings("ignore")
import UnityPy
UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.16f1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))
import store

OUT = os.path.join(os.path.dirname(store.PATH), "mapindex.json")
GAME_DIRS = [r"D:\Dofus-dofus3", os.path.expandvars(r"%LOCALAPPDATA%\Ankama\Dofus-dofus3")]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0]); ap.add_argument("--game")
    a = ap.parse_args()
    game = a.game or next((g for g in GAME_DIRS if os.path.isdir(g)), None) or sys.exit("game folder not found: use --game")
    d = os.path.join(game, "Dofus_Data", "StreamingAssets", "Content", "Map", "Data")
    files = sorted(f for f in os.listdir(d) if f.startswith("mapdata_assets_world_") and f.endswith(".bundle"))
    index, t0 = {}, time.time()
    for n, f in enumerate(files, 1):
        try:
            for o in UnityPy.load(os.path.join(d, f)).objects:
                if o.type.name != "MonoBehaviour": continue
                m = re.fullmatch(r"map_(\d+)", o.peek_name() or "")
                if m: index[m.group(1)] = f
        except Exception as e:
            print("skip", f, repr(e)[:80], file=sys.stderr)
        if n % 50 == 0: print(f"{n}/{len(files)} bundles, {len(index)} maps, {time.time() - t0:.0f}s", flush=True)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh: json.dump(index, fh, separators=(",", ":"))
    print(len(index), "maps ->", OUT)


if __name__ == "__main__":
    main()
