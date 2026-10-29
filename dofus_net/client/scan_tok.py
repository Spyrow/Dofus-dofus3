"""Read-only scan of a running Dofus.exe for 32-hex strings (ticket-shaped) and their surrounding text. Hex is masked in the output."""
import ctypes, ctypes.wintypes as wt, re, os, subprocess, collections

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "mem_tok.txt")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p), ("AllocationProtect", wt.DWORD),
                ("PartitionId", wt.WORD), ("RegionSize", ctypes.c_size_t), ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD)]
k32.OpenProcess.restype = wt.HANDLE
k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]

NUL = bytes([0])
H = b"[0-9a-f]"
def _w(b): return b"".join(bytes([c]) + NUL for c in b)
KEYS = [b'"token"', b'"ticket"', b"token=", b"ticket=", b'"apikey"', b"APIKEY: ", b'"sessionId"', b'"key":"']
RX = []
for k in KEYS:
    RX.append(("utf8", re.compile(re.escape(k) + b"[^\n]{0,90}", re.I)))
    RX.append(("utf16", re.compile(re.escape(_w(k)) + b"(?:[^" + NUL + b"]" + NUL + b"){0,60}", re.I)))

def pid():
    out = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-Process -Name Dofus).Id"], capture_output=True, text=True).stdout.split()
    return int(out[0])

def regions(h):
    a = 0; mbi = MBI()
    while k32.VirtualQueryEx(h, ctypes.c_void_p(a), ctypes.byref(mbi), ctypes.sizeof(mbi)):
        if mbi.State == 0x1000 and (mbi.Protect & 0x66) and not (mbi.Protect & 0x101):
            yield mbi.BaseAddress, mbi.RegionSize
        a = (mbi.BaseAddress or 0) + mbi.RegionSize
        if a >= 0x7fffffffffff: break

def clean(b, enc):
    t = b.decode("utf-16-le", "ignore") if enc == "utf16" else b.decode("latin1")
    return "".join(c if 32 <= ord(c) < 127 else "." for c in t)

def main():
    h = k32.OpenProcess(0x0410, False, pid())   # QUERY_INFORMATION | VM_READ only
    buf = ctypes.create_string_buffer(1 << 22); res = collections.Counter(); ex = {}
    for base, size in regions(h):
        for off in range(0, size, 1 << 22):
            n = min(1 << 22, size - off); got = ctypes.c_size_t()
            if not k32.ReadProcessMemory(h, ctypes.c_void_p(base + off), buf, n, ctypes.byref(got)) or not got.value: continue
            data = buf.raw[:got.value]
            for enc, rx in RX:
                for m in rx.finditer(data):
                    t = clean(data[max(0, m.start() - 40):m.end()], enc)
                    t = re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", "<UUID>", t)
                    t = re.sub(r"[0-9a-fA-F]{24,}", "<HEX>", t)
                    t = re.sub(r"[A-Za-z0-9+/=_-]{28,}", "<LONG>", t)
                    key = (enc, t); res[key] += 1; ex.setdefault(key, base + off + m.start())
    with open(OUT, "w", encoding="utf-8") as f:
        for (enc, t), n in res.most_common(150):
            f.write(f"{n}x {enc} @{ex[(enc, t)]:#x} {t}" + chr(10))
    print("distinct contexts:", len(res), "->", OUT)

if __name__ == "__main__": main()
