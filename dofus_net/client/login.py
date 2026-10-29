"""Headless login attempt: Zaap token -> connection server -> knc handshake. Close the real game first
(a second session on the same account will kick the first)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wire import *
from get_token import fetch_token
from dh import make_kod
KOG, KOD = 'kog', 'kod'   # obfuscated names, valid for the analysed build only

HOST, PORT = "dofus2-co-production.ankama-games.com", 5555
KNC = "knc"   # obfuscated names rotate every build: re-derive from the decoded trace after each update

def main():
    token = fetch_token().replace("-", "")
    c = GameConn(HOST, PORT)
    c.send(request(KNC, f_bytes(1, token) + f_bytes(2, "fr")))
    for _ in range(12):
        try: t, body = c.recv_message()
        except Exception as e: print("recv:", e); break
        print(t, len(body), body[:40].hex() if t not in (None,) else "")
        if t == KOG:   # server hello -> client DH public value (see dh.py)
            kod, _a = make_kod(); c.send(request(KOD, f_bytes(1, kod)))

if __name__ == "__main__":
    main()
