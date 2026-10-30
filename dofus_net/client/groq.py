"""Minimal Groq client (OpenAI-compatible chat completions, JSON mode). Used only by guide.py.

Key: env GROQ_API_KEY, or %APPDATA%/dofus_net/groq.json {"api_key": "...", "model": "..."} (outside the repo; written by save_config).
The key is never logged nor returned by the web API. What is sent to Groq: game texts (quest objectives, dialog lines, map and NPC
names, interactive element names) -- never tokens, e-mail, account or character identifiers.
Override the endpoint with GROQ_BASE_URL (tests)."""
import hashlib, json, os, threading, time, urllib.error, urllib.request
import store

CONFIG = os.path.join(os.path.dirname(store.PATH), "groq.json")
DEFAULT_MODEL = "openai/gpt-oss-120b"
MIN_INTERVAL = 1.2    # seconds between two calls (free tier rate limits)
_lock = threading.Lock(); _last = 0.0; _cache = {}


class GroqError(Exception):
    pass


def _config():
    cfg = {}
    try:
        with open(CONFIG, encoding="utf8") as f: cfg = json.load(f)
    except (OSError, ValueError): pass
    return {"api_key": os.environ.get("GROQ_API_KEY") or cfg.get("api_key"), "model": os.environ.get("GROQ_MODEL") or cfg.get("model") or DEFAULT_MODEL}


def configured():
    return bool(_config()["api_key"])


def model():
    return _config()["model"]


def save_config(api_key=None, model=None):
    cfg = {}
    try:
        with open(CONFIG, encoding="utf8") as f: cfg = json.load(f)
    except (OSError, ValueError): pass
    if api_key: cfg["api_key"] = api_key.strip()
    if model: cfg["model"] = model.strip()
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w", encoding="utf8") as f: json.dump(cfg, f)
    try: os.chmod(CONFIG, 0o600)
    except OSError: pass


def chat_json(system, user, max_tokens=300, timeout=25):
    """One chat completion in JSON mode -> parsed dict. Identical prompts are answered from a cache. Raises GroqError."""
    cfg = _config()
    if not cfg["api_key"]: raise GroqError("clé Groq absente (GROQ_API_KEY ou groq.json)")
    key = hashlib.sha256((cfg["model"] + system + user).encode()).hexdigest()
    if key in _cache: return _cache[key]
    global _last
    with _lock:
        wait = MIN_INTERVAL - (time.time() - _last)
        if wait > 0: time.sleep(wait)
        _last = time.time()
    base = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    payload = {"model": cfg["model"], "temperature": 0.1, "max_tokens": max_tokens, "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if "gpt-oss" in cfg["model"]: payload["reasoning_effort"] = "low"; payload["max_tokens"] = max_tokens + 1500   # reasoning tokens count in the limit
    body = json.dumps(payload).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body, method="POST",
                                 headers={"Authorization": "Bearer " + cfg["api_key"], "Content-Type": "application/json", "User-Agent": "dofus-net-bot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: data = json.load(r)
        out = json.loads(data["choices"][0]["message"]["content"])
    except urllib.error.HTTPError as e:
        raise GroqError(f"HTTP {e.code} : {e.read().decode('utf8', 'replace')[:160]}")
    except (urllib.error.URLError, OSError) as e: raise GroqError(f"réseau : {e}")
    except (ValueError, KeyError, IndexError) as e: raise GroqError(f"réponse inattendue : {e}")
    if not isinstance(out, dict): raise GroqError("la réponse n'est pas un objet JSON")
    _cache[key] = out
    return out
