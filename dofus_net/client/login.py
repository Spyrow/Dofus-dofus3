"""Two-step login, as observed in a full capture (tools/scan_connect.py), build 3.7.12.12.
1. connection server (dofus2-co-production:5555, plain protobuf, no Any wrapper): game token + device hash
   -> account info, then 'select server' -> one-shot ticket + game server host.
2. game server (Any-wrapped): knc(ticket, lang) -> ... kog/meq -> ksj -> ... jpx -> kod(DH, request id 2).
   Optionally kta(character id) to enter the character (the server then pushes the whole game state).
Close the real game first (a second session on the same account kicks the first)."""
import sys, os, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wire import *
from get_token import fetch_token
from dh import make_kod
import shield
# obfuscated names, valid for the analysed build only: re-derive with tools/scan_connect.py after each update
KNC, KOG, MEQ, KSJ, KSC, JPX, KOD, KTA = "knc", "kog", "meq", "ksj", "ksc", "jpx", "kod", "kta"
IZN, JOW, KJT, JPE = "izn", "jow", "kjt", "jpe"
IBV = "ibv"   # ask for the up-to-date state of a quest {3: quest id, 4: -1}; the client sends it after each icb
ITZ = "itz"   # use an interactive element {1: skill, 3: element id}
JXI, JWP = "jxi", "jwp"   # fight start (placement) / fight end (observed once)
JNS = "jns"   # one actor arriving on the map
ILT, IKY = "ilt", "iky"   # talk to an NPC (action, map, actor) / choose a dialog reply
ILX, ILF, KIS = "ilx", "ilf", "kis"   # dialog opened / next dialog step (replies + message) / dialog closed
ICA, ICJ, ICB, IBG = "ica", "icj", "icb", "ibg"   # quest lists at login / quest state / quest changed / objective done
JOK, JNY, JOC, JNZ, JNJ = "jok", "jny", "joc", "jnz", "jnj"   # move (path keys + map id), movement done, change map (neighbour id), actor path, map actors   # izn = last message of the character-selection burst; jow = map to load
# requests the real client sends once the character is selected, ending with kkc (= world loaded, server then pushes the map)
READY = [("lnt", f_varint(1, 1)), ("kne", f_varint(2, 1)), ("kkn", b""), ("kkt", b""), ("klu", b""), ("klx", b""), ("kmj", b""),
         ("jgm", f_varint(1, 3)), ("jgm", b""), ("jcy", b""), ("kkc", b"")]

HOST, PORT = "dofus2-co-production.ankama-games.com", 5555
CLIENT_VERSION = "3.7.12.12"
SERVER_ID = 291     # id sent in the 'select server' request in the capture (ticket host: dofus2-ga-imagiro)

def _field(body, num):
    return next((v for f, v in parse(body) if f == num), None)

def connection_step(token, device_hash, server_id=SERVER_ID):
    """-> (ticket, game_host). device_hash: 64 hex chars (the capture shows an upper-case sha256)."""
    c = GameConn(HOST, PORT)
    auth = f_bytes(1, f_bytes(1, "0") + f_bytes(3, f_bytes(1, device_hash.upper()) + f_bytes(3, f_bytes(1, token) + f_bytes(2, b"")) + f_bytes(5, CLIENT_VERSION)))
    c.send(auth)
    r = c.recv_frame()
    if _field(r, 2) is None: raise SystemExit("connection server refused the auth step (device hash / token?)")
    c.send(f_bytes(1, f_bytes(1, "1") + f_bytes(4, f_varint(1, server_id))))   # request id "1"
    r = _field(c.recv_frame(), 2) or sys.exit("no server-selection reply")
    sel = _field(_field(r, 4) or sys.exit("no ticket in reply"), 1)
    ticket, host = _field(sel, 1).decode(), _field(sel, 2).decode()
    c.s.close()
    return ticket, host

def game_step(ticket, host, enter=False, idle=15, collect=None, quiet=False, enter_map=False, max_seconds=None,
              stop=None, heartbeat=0, on_message=None, log=None, on_ready=None):
    """Handshake (+ character selection if enter). collect: list that receives every (type, body) the server pushes;
    on_message(type, body) is called for each one. enter_map: after the selection burst, send the client's READY requests
    so the server also pushes the current map (jow). heartbeat: send the client's keep-alive (kne) every N seconds.
    log(level, text): connection/action journal. Returns the connection once `stop` is set, the server has been silent
    for `idle` seconds (None = never), closed it, or max_seconds elapsed."""
    import socket, time
    log = log or (lambda level, text: None)
    t0 = last_rx = last_hb = time.time()
    log("info", f"Connexion au serveur de jeu {host}:{PORT}")
    c = GameConn(host, PORT); c.s.settimeout(1.0)
    if on_ready: on_ready(c)
    c.send(request(KNC, f_bytes(1, ticket) + f_bytes(2, "fr"))); log("action", "→ knc (ticket)")
    char_id = None; sent_kod = False
    while True:
        now = time.time()
        if (stop is not None and stop.is_set()) or (max_seconds and now - t0 > max_seconds): break
        if heartbeat and now - last_hb >= heartbeat: c.send(request("kne", f_varint(2, 1))); last_hb = now
        try: t, body = c.recv_message()
        except socket.timeout:
            if idle and time.time() - last_rx > idle:
                if not quiet: print("recv: idle")
                break
            continue
        except Exception as e:
            log("warn", f"Connexion fermée : {e}")
            if not quiet: print("recv:", e)
            break
        last_rx = time.time()
        if collect is not None: collect.append((t, body))
        if on_message: on_message(t, body)
        if not quiet: print(t, len(body))
        if t == IZN and enter_map and enter:
            for name, b in READY: c.send(request(name, b))
            log("action", "→ requêtes « monde chargé » (lnt … kkc)")
        elif t == JOW and enter_map:   # ack the map load like the client (kjt + jpe)
            mid = next((v for f, v in parse(body) if f == 1 and isinstance(v, int)), None)
            if mid: c.send(request(KJT, f_varint(1, mid))); c.send(request(JPE, f_varint(1, mid))); log("action", f"→ carte {mid} chargée (kjt + jpe)")
        if t == MEQ: c.send(request(KSJ)); log("action", "→ ksj")
        elif t == KSC:   # {2: {3: character id, ...}}
            sub = _field(body, 2)
            if sub is not None: char_id = _field_varint(sub, 3)
        elif t == JPX and not sent_kod:
            kod, _a = make_kod(); c.send(request(KOD, f_bytes(1, kod), req_id=2)); sent_kod = True; log("action", "→ kod (échange DH)")
            if enter and char_id: c.send(request(KTA, f_varint(1, char_id))); log("action", f"→ kta (sélection du personnage {char_id})")
    return c

def _field_varint(body, num):
    return next((v for f, v in parse(body) if f == num and isinstance(v, int)), None)

def main(token=None, enter=False):
    token = token or fetch_token()   # 36-char game token, dashes kept
    cert_id, cert_hash = shield.certificate_args()
    if not cert_hash: raise SystemExit("no Shield certificate: run shield.py request / validate first")
    ticket, host = connection_step(token, cert_hash)
    print("ticket length:", len(ticket), "game host:", host)   # never print the ticket itself
    game_step(ticket, host, enter)

if __name__ == "__main__":
    main(enter="--enter" in sys.argv)
