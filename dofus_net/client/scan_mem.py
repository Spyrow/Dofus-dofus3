"""Read-only scan of a running Dofus.exe for plaintext HAAPI traces (URLs/headers/JSON). Secrets are masked in the output."""
import ctypes, ctypes.wintypes as wt, re, subprocess, sys, time, os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mem_scan.txt")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p), ("AllocationProtect", wt.DWORD),
                ("PartitionId", wt.WORD), ("RegionSize", ctypes.c_size_t), ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD)]
k32.OpenProcess.restype = wt.HANDLE
k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]

def dofus_pid():
    out = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-Process -Name Dofus -ErrorAction SilentlyContinue).Id"], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None

def connected(pid):
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, encoding="cp850", errors="replace").stdout
    return any(f":5555 " in l and "ESTABLISHED" in l and l.split()[-1] == str(pid) for l in out.splitlines())

def mask(s):
    s = re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", "<UUID>", s)
    s = re.sub(r"\b[0-9a-fA-F]{32,}\b", "<HEX>", s)
    s = re.sub(r"[A-Za-z0-9+/=_-]{40,}", "<LONG>", s)
    return re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<EMAIL>", s)

def regions(h):
    a = 0; mbi = MBI()
    while k32.VirtualQueryEx(h, ctypes.c_void_p(a), ctypes.byref(mbi), ctypes.sizeof(mbi)):
        if mbi.State == 0x1000 and (mbi.Protect & 0x66) and not (mbi.Protect & 0x101):
            yield mbi.BaseAddress, mbi.RegionSize
        a = (mbi.BaseAddress or 0) + mbi.RegionSize
        if a >= 0x7fffffffffff: break

def main():
    pid = dofus_pid()
    while not pid: time.sleep(2); pid = dofus_pid()
    print("Dofus pid", pid, "- waiting for the game-server connection (log in now)", flush=True)
    while not connected(pid): time.sleep(2)
    time.sleep(4)
    h = k32.OpenProcess(0x0410, False, pid)   # QUERY_INFORMATION | VM_READ only
    needles = {"haapi": b"haapi.ankama.com", "apikey": b"APIKEY", "createtoken": b"CreateToken", "ticket": b"ticket"}
    pats = []
    for n, b in needles.items():
        pats.append((n, "utf8", re.compile(re.escape(b), re.I))); pats.append((n, "utf16", re.compile(re.escape(b.decode().encode("utf-16-le")), re.I)))
    seen = set(); lines = []; buf = ctypes.create_string_buffer(1 << 22)
    for base, size in regions(h):
        for off in range(0, size, 1 << 22):
            n = min(1 << 22, size - off); got = ctypes.c_size_t()
            if not k32.ReadProcessMemory(h, ctypes.c_void_p(base + off), buf, n, ctypes.byref(got)) or not got.value: continue
            data = buf.raw[:got.value]
            for name, enc, rx in pats:
                for m in rx.finditer(data):
                    s, e = max(0, m.start() - 160), min(len(data), m.end() + 240)
                    chunk = data[s:e]
                    txt = chunk.decode("utf-16-le", "ignore") if enc == "utf16" else chunk.decode("latin1")
                    txt = mask("".join(c if 32 <= ord(c) < 127 else "." for c in txt))
                    key = txt[:120]
                    if key in seen: continue
                    seen.add(key); lines.append(f"[{name}/{enc} @{base+off+m.start():#x}] {txt}")
    open(OUT, "w", encoding="utf-8").write("\n".join(lines))
    print("done:", len(lines), "unique contexts ->", OUT)

if __name__ == "__main__": main()
