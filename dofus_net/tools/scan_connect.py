"""Capture the Dofus connection phase from before the game starts, then list what the client sends/receives
and which message names client/login.py does not know yet (obfuscated names rotate every build).

Usage (run BEFORE launching the game; dumpcap usually needs an elevated shell / Npcap):
    python tools/scan_connect.py              # wait for Dofus.exe, capture until it exits (or Ctrl-C)
    python tools/scan_connect.py --decode f.pcapng   # only analyse an existing capture
Output: connect_scan.pcapng + connect_scan.txt next to this script's parent folder."""
import argparse, os, re, shutil, subprocess, sys, time, json, collections

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
PCAP = os.path.join(ROOT, "connect_scan.pcapng")
REPORT = os.path.join(ROOT, "connect_scan.txt")
PORTS = [5555]
WS_DIRS = [r"D:\Wireshark", r"C:\Program Files\Wireshark", r"C:\Program Files (x86)\Wireshark"]

def find_tool(name):
    p = shutil.which(name)
    if p: return p
    for d in WS_DIRS:
        c = os.path.join(d, name + ".exe")
        if os.path.exists(c): return c
    sys.exit(f"{name} not found: install Wireshark (with Npcap) or add it to PATH")

def dofus_running():
    out = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-Process -Name Dofus -ErrorAction SilentlyContinue).Id"],
                         capture_output=True, text=True).stdout.split()
    return bool(out)

def capture_args(dumpcap):
    out = subprocess.run([dumpcap, "-D"], capture_output=True, text=True).stdout
    ifs = [l.split(".", 1)[0] for l in out.splitlines() if l.strip() and not re.search(r"loopback|etw|wifidirect", l, re.I)]
    if not ifs: sys.exit("no capture interface found (is Npcap installed?)")
    flt = "tcp and (" + " or ".join(f"port {p}" for p in PORTS) + ")"
    args = [dumpcap]
    for i in ifs: args += ["-i", i, "-f", flt]   # -f is per interface: it must follow each -i
    return args + ["-w", PCAP]

def run_capture():
    dumpcap = find_tool("dumpcap")
    if os.path.exists(PCAP): os.remove(PCAP)
    cap = subprocess.Popen(capture_args(dumpcap), stderr=subprocess.PIPE, text=True)
    time.sleep(2)
    if cap.poll() is not None: sys.exit("dumpcap failed: " + cap.stderr.read() + "\n(try an elevated shell)")
    print("Capture running -> launch the game NOW (log in to the character screen), then close it or press Ctrl-C.", flush=True)
    try:
        while not dofus_running(): time.sleep(1)
        while dofus_running(): time.sleep(1)
        time.sleep(3)
    except KeyboardInterrupt:
        pass
    finally:
        cap.terminate(); cap.wait()
    return PCAP

def known_names():
    src = open(os.path.join(ROOT, "client", "login.py"), encoding="utf-8").read()
    return set(re.findall(r"""['"]([a-z]{3})['"]""", src))

def analyse(pcap):
    import decode_pcap as d
    d.TSHARK = find_tool("tshark")
    known = known_names()
    per = []   # (t, stream, direction, frame)
    for sid, dirs in d.streams(pcap, PORTS).items():
        for dr, pk in dirs.items():
            for t, fr in d.frames(pk): per.append((t, sid, dr, fr))
    per.sort(key=lambda x: x[0])
    lines, seen = [], collections.OrderedDict()
    for t, sid, dr, fr in per:
        r = json.dumps(d.render(fr), ensure_ascii=False)
        m = re.search(r"type\.ankama\.com/(\w+)", r)
        name = m.group(1) if m else "?"
        out = ">" if dr.split("->")[0].rsplit(":", 1)[1] != "5555" else "<"
        lines.append(f"{t:9.3f} s{sid} {out} {name:<6} [{len(fr)}] {r[:300]}")
        seen.setdefault((sid, out, name), t)
    rep = ["== Connection-phase messages (first 25 per stream, in order) =="]
    cnt = collections.Counter()
    for l in lines:
        sid = l.split()[1]
        cnt[sid] += 1
        if cnt[sid] <= 25: rep.append(l)
    rep.append("\n== Names used by client/login.py: " + ", ".join(sorted(known)) + " ==")
    names = {n for (_, _, n) in seen}
    miss = sorted(k for k in known if k not in names)
    rep.append("Absent from this capture (build rotated, or handshake not captured): " + (", ".join(miss) or "none"))
    c2s = [(sid, n, t) for (sid, o, n), t in seen.items() if o == ">"]
    rep.append("\nClient->server message names by first appearance (login candidates = earliest, before any request ids >=0):")
    for sid, n, t in sorted(c2s, key=lambda x: x[2])[:15]: rep.append(f"  {t:8.3f}s s{sid} {n}")
    rep.append("\nFull decoded stream:\n" + "\n".join(lines))
    open(REPORT, "w", encoding="utf-8").write("\n".join(rep))
    print("\n".join(rep[:60]))
    print(f"\nreport -> {REPORT}")

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--decode", help="analyse an existing pcapng instead of capturing")
    a = ap.parse_args()
    analyse(a.decode or run_capture())

if __name__ == "__main__":
    main()
