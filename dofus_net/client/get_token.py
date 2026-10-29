"""Ask the running Ankama launcher for a Dofus 3 game token, using the --port/--hash/--instanceId of a launched Dofus.exe."""
import re, subprocess, sys
sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")
from zaap import Zaap

def launch_args():
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
        "(Get-CimInstance Win32_Process -Filter \"Name='Dofus.exe'\").CommandLine"], capture_output=True, text=True).stdout
    g = lambda k: re.search(rf"--{k} (\S+)", out)
    if not (g("port") and g("hash")): raise SystemExit("No running Dofus.exe with Zaap arguments")
    return int(g("port").group(1)), g("hash").group(1), int(g("instanceId").group(1))

def fetch_token(port=None, hash_=None, instance=None):
    if port is None: port, hash_, instance = launch_args()
    z = Zaap(port, instance_id=instance)
    z.connect(hash_)
    return z.game_token(hash_)

if __name__ == "__main__":
    t = fetch_token()
    print("token length:", len(t) if t else None)
