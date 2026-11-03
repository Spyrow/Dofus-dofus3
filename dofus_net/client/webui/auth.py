"""Admin login for the web UI when it is put online (behind HTTPS: reverse proxy or tunnel on the same machine).

Enabled when DOFUS_ADMIN_PASSWORD_HASH is set; without it the server stays local-only as before (no login, Host must be 127.0.0.1 / localhost).
  DOFUS_ADMIN_USER           admin user name (default "admin")
  DOFUS_ADMIN_PASSWORD_HASH  output of `python webui/auth.py hash` (PBKDF2-SHA256, salted); the password itself is never stored
  DOFUS_SESSION_SECRET       key that signs the session cookie (default: random at each start, which logs everybody out on restart)
  DOFUS_PUBLIC_HOST          public host name served by the proxy (e.g. bot.example.com): the only extra Host / Origin accepted
The session cookie is HttpOnly, SameSite=Strict, Secure, signed (HMAC-SHA256) and expires after 12 hours; failed logins are throttled per address."""
import base64, getpass, hashlib, hmac, os, secrets, sys, time

ITERATIONS = 600_000
SESSION_SECONDS = 12 * 3600
MAX_FAILS, FAIL_WINDOW = 5, 300
_secret = (os.environ.get("DOFUS_SESSION_SECRET") or secrets.token_hex(32)).encode()
_fails = {}


def enabled(): return bool(os.environ.get("DOFUS_ADMIN_PASSWORD_HASH"))


def public_host(): return os.environ.get("DOFUS_PUBLIC_HOST") or None


def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2${ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def _check_password(password, stored):
    try:
        _alg, it, salt, dk = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError): return False


def throttled(addr):
    now = time.time(); _fails[addr] = [t for t in _fails.get(addr, []) if now - t < FAIL_WINDOW]
    return len(_fails[addr]) >= MAX_FAILS


def check_login(addr, user, password):
    """True for the admin's user name + password. Always spends the hashing time, so a wrong user name is not distinguishable."""
    ok_pw = _check_password(password or "", os.environ.get("DOFUS_ADMIN_PASSWORD_HASH", ""))
    ok_user = hmac.compare_digest((user or "").encode(), os.environ.get("DOFUS_ADMIN_USER", "admin").encode())
    if ok_pw and ok_user: _fails.pop(addr, None); return True
    _fails.setdefault(addr, []).append(time.time()); return False


def _sign(payload): return hmac.new(_secret, payload.encode(), hashlib.sha256).hexdigest()


def make_cookie():
    exp = str(int(time.time()) + SESSION_SECONDS); return f"{exp}.{_sign(exp)}"


def valid_cookie(value):
    try:
        exp, sig = (value or "").split(".", 1)
        return int(exp) > time.time() and hmac.compare_digest(sig, _sign(exp))
    except ValueError: return False


LOGIN_PAGE = """<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Connexion</title>
<style>body{font:16px system-ui;background:#14161a;color:#e8e8e8;display:grid;place-items:center;min-height:100vh;margin:0}
form{background:#1f2329;padding:28px;border-radius:12px;width:min(320px,86vw);display:grid;gap:12px}
input,button{font:inherit;padding:10px;border-radius:8px;border:1px solid #3a414b;background:#14161a;color:inherit}button{background:#3b82f6;border:0;cursor:pointer}
.e{color:#f87171;min-height:1.2em;font-size:14px}</style>
<form method="post" action="/login"><h2 style="margin:0">Administration</h2><input name="user" placeholder="Identifiant" autocomplete="username" required>
<input name="password" type="password" placeholder="Mot de passe" autocomplete="current-password" required><div class="e">%s</div><button>Se connecter</button></form></html>"""


if __name__ == "__main__":
    if sys.argv[1:] == ["hash"]:
        pw = getpass.getpass("Mot de passe admin : ")
        if len(pw) < 12 or pw != getpass.getpass("Confirmer : "): sys.exit("12 caractères minimum, et les deux saisies doivent être identiques")
        print("DOFUS_ADMIN_PASSWORD_HASH='" + hash_password(pw) + "'")
    else: sys.exit("usage : python webui/auth.py hash")
