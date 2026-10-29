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

def game_step(ticket, host, enter=False, listen=20):
    c = GameConn(host, PORT)
    c.send(request(KNC, f_bytes(1, ticket) + f_bytes(2, "fr")))
    char_id = None; sent_kod = False
    for _ in range(listen * 10):
        try: t, body = c.recv_message()
        except Exception as e: print("recv:", e); break
        print(t, len(body))
        if t == MEQ: c.send(request(KSJ))
        elif t == KSC:   # {2: {3: character id, ...}}
            sub = _field(body, 2)
            if sub is not None: char_id = _field_varint(sub, 3)
        elif t == JPX and not sent_kod:
            kod, _a = make_kod(); c.send(request(KOD, f_bytes(1, kod), req_id=2)); sent_kod = True
            if enter and char_id: c.send(request(KTA, f_varint(1, char_id)))
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
