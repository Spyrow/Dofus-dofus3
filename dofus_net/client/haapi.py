"""HAAPI calls the launcher makes with the OAuth access_token (it uses it directly as the APIKEY)."""
import json, urllib.parse, urllib.request

HAAPI_URL = "https://haapi.ankama.com/"
GAME_IDS = {"dofus": 1, "wakfu": 3, "krosmaga": 17, "waven": 22}   # launcher GAMES_NAME; 99 = launcher chat, 102 = Zaap itself


def create_game_token(api_key, game_id=GAME_IDS["dofus"]):
    """GET Account/CreateToken?game=<id> with header APIKEY -> one-shot game token (same as Zaap auth_getGameToken)."""
    url = HAAPI_URL + "json/Ankama/v5/Account/CreateToken?" + urllib.parse.urlencode({"game": game_id})
    req = urllib.request.Request(url, headers={"APIKEY": api_key, "User-Agent": "Zaap pkce_login"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)["token"]
