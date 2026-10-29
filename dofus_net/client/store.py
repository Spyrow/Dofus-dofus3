"""Persist the HAAPI key + refresh token OUTSIDE the repo (%APPDATA%/dofus_net/tokens.json, or ~/.dofus_net_tokens.json)."""
import json, os

PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "dofus_net", "tokens.json")

def load():
    try:
        with open(PATH, encoding="utf8") as f: return json.load(f)
    except (OSError, ValueError): return None

def save(key, refresh_token):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(PATH, "w", encoding="utf8") as f: json.dump({"key": key, "refresh_token": refresh_token}, f)
