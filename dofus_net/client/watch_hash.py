"""Wait for a launcher-started Dofus.exe, save its Zaap args to session.json, then test whether the hash outlives the process."""
import sys, os, json, time, re, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zaap import Zaap
HERE = os.path.dirname(os.path.abspath(__file__))

def cmdline():
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
        "(Get-CimInstance Win32_Process -Filter \"Name='Dofus.exe'\").CommandLine"], capture_output=True, text=True).stdout
    return out

def args(out):
    g = lambda k: re.search(rf"--{k} (\S+)", out)
    return (int(g("port").group(1)), g("hash").group(1), int(g("instanceId").group(1))) if g("port") and g("hash") else None

while not (a := args(cmdline())): time.sleep(1)
port, h, inst = a
json.dump({"port": port, "hash": h, "instance": inst}, open(os.path.join(HERE, "session.json"), "w"))
print("captured; waiting for Dofus.exe to exit", flush=True)
while args(cmdline()): time.sleep(1)
time.sleep(2)
try:
    z = Zaap(port, instance_id=inst); z.connect(h); t = z.game_token(h)
    print("hash STILL VALID after exit; token length", len(t) if t else None)
except Exception as e:
    print("hash INVALID after exit:", repr(e))
