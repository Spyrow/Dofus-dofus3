"""Extract the character snapshot (name, characteristics) from a capture of a full game session.

    python tools/extract_state.py connect_scan.pcapng                 # print the snapshot as JSON
    python tools/extract_state.py connect_scan.pcapng --account ID    # also write it as the web-UI snapshot of that account
Needs the capture to include the character selection (see tools/scan_connect.py)."""
import argparse, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(os.path.dirname(HERE), "client"))
import decode_pcap as d
from scan_connect import find_tool
import gamestate
from wire import parse


def server_messages(pcap):
    d.TSHARK = find_tool("tshark")
    out = []
    for dirs in d.streams(pcap, [5555]).values():
        for direction, pk in dirs.items():
            if direction.split("->")[0].rsplit(":", 1)[1] != "5555": continue    # server -> client only
            for t, fr in d.frames(pk):
                try:
                    for f, v in parse(fr):
                        if f != 3: continue
                        for f2, a in parse(v):
                            if f2 == 1:
                                dd = dict(parse(a)); out.append((t, dd[1].decode().split("/", 1)[1], dd.get(2, b"")))
                except (ValueError, IndexError, KeyError, UnicodeDecodeError): continue
    return [(n, b) for _t, n, b in sorted(out, key=lambda x: x[0])]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pcap"); ap.add_argument("--account", help="account id (accounts.json) to write the snapshot for")
    a = ap.parse_args()
    snap = gamestate.snapshot(server_messages(a.pcap))
    if not snap.get("characteristics"): sys.exit("no characteristics message in this capture (was the character entered?)")
    print(json.dumps(snap, ensure_ascii=False, indent=1))
    if a.account:
        import accounts
        accounts.save_character(a.account, snap); print("written ->", accounts.char_path(a.account), file=sys.stderr)


if __name__ == "__main__":
    main()
