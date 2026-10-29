"""Login -> HAAPI game token for Dofus (game id 1) -> handshake with the connection server (login.py).
Default: PKCE login in the browser. --headless: login/password via HAAPI CreateApiKey, no UI
(ANKAMA_LOGIN / ANKAMA_PASSWORD env vars, or prompted).

Old: PKCE login (browser) -> HAAPI game token for Dofus (game id 1) -> handshake with the connection server (login.py).
Close the real game/launcher first: port 9001 must be free, and a second session kicks the first."""
import argparse, getpass, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pkce_login import login
from haapi import create_game_token, create_api_key, GAME_IDS
import login as game_login

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--provider", default="ankama")
    ap.add_argument("--game-id", type=int, default=GAME_IDS["dofus"])
    ap.add_argument("--headless", action="store_true", help="login/password instead of the browser")
    a = ap.parse_args()
    if a.headless:
        user = os.environ.get("ANKAMA_LOGIN") or input("login: ")
        pwd = os.environ.get("ANKAMA_PASSWORD") or getpass.getpass("password: ")
        r = create_api_key(user, pwd)
        print("security_state:", (r.get("data") or {}).get("security_state"), file=sys.stderr)
        access = r["key"]
    else:
        access = login(a.provider)["access_token"]
    token = create_game_token(access, a.game_id)
    print("game token length:", len(token), file=sys.stderr)   # never print the token itself
    game_login.main(token)

if __name__ == "__main__":
    main()
