"""Ankama Shield: register this script as a trusted device (certificate), as the launcher does on first login.

  python shield.py request          # e-mails a security code to the account
  python shield.py validate <CODE>  # exchanges it for a certificate, stored in %APPDATA%/dofus_net/cert.json

hm1/hm2 are device fingerprints. The launcher derives them from arch/platform/machine id/user/OS/RAM; the server
only needs them to stay the same between ValidateCode and the later hash, so this tool uses its own stable
fingerprint and its own device name (it shows up as such in the account's trusted devices).
hash = sha256(hm1 + AES-256-ECB-decrypt(encodedCertificate, key=hm2)).hexdigest()  (cryptoHelper.generateHashFromCertif)
"""
import base64, getpass, hashlib, json, os, platform, sys, urllib.error, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
from haapi import HAAPI_URL, ZAAP_GAME_ID

CERT_PATH = os.path.join(os.path.dirname(store.PATH), "cert.json")
DEVICE_NAME = "dofus_net-" + getpass.getuser()


def fingerprint():
    h = hashlib.sha256((platform.machine() + platform.system() + platform.node() + getpass.getuser()).encode()).hexdigest()[:32]
    return h, h[::-1]                                   # hm1, hm2 (hm2 = reversed hm1, like the launcher)


def _get(path, key, **params):
    url = HAAPI_URL + path + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"APIKEY": key, "User-Agent": "Zaap pkce_login"}), timeout=20) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HAAPI {e.code}: {e.read().decode()[:300]}")


def request_code(key, sms=False):
    return _get("json/Ankama/v5/Shield/SecurityCode", key, transportType="SMS" if sms else "EMAIL")


def validate_code(key, code):
    hm1, hm2 = fingerprint()
    _, body = _get("json/Ankama/v5/Shield/ValidateCode", key, game_id=ZAAP_GAME_ID, code=code, hm1=hm1, hm2=hm2, name=DEVICE_NAME)
    os.makedirs(os.path.dirname(CERT_PATH), exist_ok=True)
    with open(CERT_PATH, "w", encoding="utf8") as f:
        json.dump({"id": body["id"], "encodedCertificate": body["encodedCertificate"]}, f)
    return body["id"]


def certificate_args():
    """(certificate_id, certificate_hash) for CreateToken, or (None, None) if no certificate is stored."""
    try:
        with open(CERT_PATH, encoding="utf8") as f: c = json.load(f)
    except (OSError, ValueError): return None, None
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes   # pip install cryptography
    hm1, hm2 = fingerprint()
    d = Cipher(algorithms.AES(hm2.encode()), modes.ECB()).decryptor()
    raw = d.update(base64.b64decode(c["encodedCertificate"])) + d.finalize()
    raw = raw[:-raw[-1]]                                # PKCS7 unpad
    return c["id"], hashlib.sha256(hm1.encode() + raw).hexdigest()


if __name__ == "__main__":
    s = store.load() or sys.exit("no saved tokens: run connect.py once (browser login)")
    if sys.argv[1:2] == ["request"]:
        print("code requested:", request_code(s["key"]))
    elif sys.argv[1:2] == ["validate"] and len(sys.argv) == 3:
        print("certificate id:", validate_code(s["key"], sys.argv[2]))
    else:
        sys.exit(__doc__)
