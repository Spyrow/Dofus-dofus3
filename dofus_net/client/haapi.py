"""HAAPI calls the launcher makes with the OAuth access_token (it uses it directly as the APIKEY)."""
import json, urllib.error, urllib.parse, urllib.request

HAAPI_URL = "https://haapi.ankama.com/"
GAME_IDS = {"dofus": 1, "wakfu": 3, "krosmaga": 17, "waven": 22}   # launcher GAMES_NAME; 99 = launcher chat, 102 = Zaap itself


def create_game_token(api_key, game_id=GAME_IDS["dofus"]):
    """GET Account/CreateToken?game=<id> with header APIKEY -> one-shot game token (same as Zaap auth_getGameToken)."""
    url = HAAPI_URL + "json/Ankama/v5/Account/CreateToken?" + urllib.parse.urlencode({"game": game_id})
    req = urllib.request.Request(url, headers={"APIKEY": api_key, "User-Agent": "Zaap pkce_login"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)["token"]


ZAAP_GAME_ID = 102
SHOP_KEY, PAYMENT_MODE = "ZAAP", "OK"          # SHOP_HAAPI_KEYS in the launcher


def create_api_key(login, password, lang="fr", certificate_id=None, certificate_hash=None):
    """POST Api/CreateApiKey (the launcher's login/password path) -> {key, account_id, refresh_token, data}.
    `key` is what goes in the APIKEY header. Accounts needing Shield/certificate answer with
    data.security_state (e.g. UNSECURED) or an HTTP error: certificate_* is not handled here.
    Observed 2026-10-29: for a regular (non-guest) account it answers 422 "Guest account only"
    (and 422 "Missing required parameters 'login'" if the body is JSON, so it is form-encoded):
    regular accounts must go through the PKCE flow in pkce_login.py."""
    body = {"login": login, "password": password, "game_id": ZAAP_GAME_ID, "long_life_token": True,
            "shop_key": SHOP_KEY, "payment_mode": PAYMENT_MODE, "lang": lang}
    if certificate_id and certificate_hash:
        body.update(certificate_id=certificate_id, certificate_hash=certificate_hash)
    body["long_life_token"] = "true"
    req = urllib.request.Request(HAAPI_URL + "json/Ankama/v5/Api/CreateApiKey", urllib.parse.urlencode(body).encode(),
                                 {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Zaap pkce_login"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HAAPI {e.code}: {e.read().decode()[:300]}")


def refresh_api_key(key, refresh_token, lang="fr"):
    """POST Api/RefreshApiKey (header APIKEY = current key) -> {key, account_id, refresh_token}.
    The refresh_token is rotated on every call: persist the new one."""
    body = urllib.parse.urlencode({"refresh_token": refresh_token, "long_life_token": "true",
                                   "shop_key": SHOP_KEY, "payment_mode": PAYMENT_MODE, "lang": lang}).encode()
    req = urllib.request.Request(HAAPI_URL + "json/Ankama/v5/Api/RefreshApiKey", body, {
        "Content-Type": "application/x-www-form-urlencoded", "APIKEY": key, "User-Agent": "Zaap pkce_login"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HAAPI {e.code}: {e.read().decode()[:300]}")
