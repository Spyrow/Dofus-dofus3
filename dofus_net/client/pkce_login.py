"""OAuth2 authorization-code + PKCE login, as done by the Ankama launcher (Zaap).

Mirrors the launcher's auth class (main.js, handleProvider / getAccessTokenWithCode):
  1. random code_verifier (43..127 chars of [A-Za-z0-9-._~])
  2. code_challenge = base64url(SHA256(verifier)), no padding
  3. open  {AUTH_URL}login/{provider}?code_challenge=...&redirect_uri=...&client_id=102&direct=true
  4. catch ?code=... on http://127.0.0.1:9001/authorized
  5. POST  {AUTH_URL}token  (grant_type=authorization_code, code, redirect_uri, client_id, code_verifier)

Use it with your own account only. The token is printed to stdout, or written to --out (keep that file
out of git).  Provider names are not verified here ("ankama" is a guess; the launcher passes the one
chosen in its login UI).
"""
import argparse, base64, hashlib, json, secrets, sys, threading, urllib.parse, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AUTH_URL = "https://auth.ankama.com/"
CLIENT_ID = 102                       # ZAAP_GAME_ID in the launcher
PORT = 9001
REDIRECT_URI = f"http://127.0.0.1:{PORT}/authorized"
VERIFIER_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"


def code_verifier():
    return "".join(secrets.choice(VERIFIER_CHARS) for _ in range(43 + secrets.randbelow(85)))


def code_challenge(verifier):
    d = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(d).decode().rstrip("=")


def wait_for_code(timeout):
    box = {}
    done = threading.Event()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if "code" in q:
                box["code"] = q["code"][0]
                done.set()
            self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8"); self.end_headers()
            self.wfile.write(b"OK, you can close this tab." if "code" in q else b"waiting for ?code=")
        def log_message(self, *a): pass

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return box, done, srv


def exchange(code, verifier):
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID, "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(AUTH_URL + "token", body, {
        "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Zaap pkce_login"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def login(provider="ankama", timeout=300):
    """Interactive browser login; returns the /token response (access_token, refresh_token, ...)."""
    verifier = code_verifier()
    box, done, srv = wait_for_code(timeout)
    qs = urllib.parse.urlencode({
        "code_challenge": code_challenge(verifier), "redirect_uri": REDIRECT_URI, "client_id": CLIENT_ID,
        "direct": "true", "origin_tracker": "https://www.ankama-launcher.com/launcher"})
    url = f"{AUTH_URL}login/{provider}?{qs}"
    print("Opening", url, file=sys.stderr)
    webbrowser.open(url)
    try:
        if not done.wait(timeout):
            sys.exit("timeout waiting for the redirect")
    finally:
        srv.shutdown()
    return exchange(box["code"], verifier)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--provider", default="ankama")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out", help="write the token response to this JSON file instead of stdout")
    ap.add_argument("--selftest", action="store_true", help="check the PKCE maths against RFC 7636 and exit")
    a = ap.parse_args()

    if a.selftest:
        assert code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        print("PKCE ok"); return

    tok = login(a.provider, a.timeout)
    out = json.dumps(tok, indent=2)
    if a.out:
        open(a.out, "w").write(out); print("written to", a.out, file=sys.stderr)
    else:
        print(out)


if __name__ == "__main__":
    main()
