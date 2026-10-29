"""Build address index from il2cpp.json and find direct callers of a method in GameAssembly.dll."""
import json, pickle, os, re, sys, struct, bisect
import pefile
BASE = 0x180000000
DLL = r"C:\Users\alex\AppData\Local\Ankama\Dofus-dofus3\GameAssembly.dll"
PKL = r"C:\Users\alex\dofus_net\re\index.pkl"

def build():
    d = json.load(open(r"C:\Users\alex\dofus_meta\il2cpp.json", encoding="utf-8"))["addressMap"]
    m = sorted((int(x["virtualAddress"], 16), x["group"] + "::" + x["dotNetSignature"]) for x in d["methodDefinitions"])
    s = {int(x["virtualAddress"], 16): x["string"] for x in d["stringLiterals"]}
    pickle.dump((m, s), open(PKL, "wb"))

def load():
    if not os.path.exists(PKL): build()
    return pickle.load(open(PKL, "rb"))

def name_of(methods, addrs, a):
    i = bisect.bisect_right(addrs, a) - 1
    return (methods[i][1], a - methods[i][0]) if i >= 0 else (None, 0)

class Img:
    def __init__(self):
        self.pe = pefile.PE(DLL, fast_load=True); self.data = open(DLL, "rb").read()
        self.text = next(s for s in self.pe.sections if s.Name.startswith(b"il2cpp"))
        self.tb = BASE + self.text.VirtualAddress
        self.tdata = self.data[self.text.PointerToRawData:self.text.PointerToRawData + self.text.SizeOfRawData]
    def callers(self, targets):
        """Scan .text for E8 rel32 calls / E9 jumps landing in targets -> {target: [call_site_va]}"""
        out = {t: [] for t in targets}; tset = set(targets); b = self.tdata; i = 0
        while True:
            i = b.find(b"\xe8", i)
            if i < 0 or i + 5 > len(b): break
            rel, = struct.unpack_from("<i", b, i + 1); dst = self.tb + i + 5 + rel
            if dst in tset: out[dst].append(self.tb + i)
            i += 1
        return out
    def read(self, va, n):
        off = self.pe.get_offset_from_rva(va - BASE); return self.data[off:off + n]

if __name__ == "__main__":
    pat = re.compile(sys.argv[1]); methods, strs = load(); addrs = [a for a, _ in methods]
    tg = [a for a, n in methods if pat.search(n)]
    print(len(tg), "target methods"); img = Img(); res = img.callers(tg)
    for t in tg:
        print(hex(t), dict(methods)[t])
        seen = {}
        for c in res[t]:
            n, off = name_of(methods, addrs, c); seen.setdefault(n, []).append(hex(c))
        for n, cs in seen.items(): print("    called from", n, cs[:2])

def xrefs(img, targets, chunk=1 << 24):
    """RIP-relative references (disp32) from the il2cpp code section to target VAs -> {target: [site VA]}"""
    import numpy as np
    out = {t: [] for t in targets}; tl = np.array(sorted(targets), dtype=np.int64)
    raw = np.frombuffer(img.tdata, dtype=np.uint8)
    for st in range(0, len(raw) - 4, chunk):
        b = raw[st:st + chunk + 4]; n = len(b) - 4
        d = (b[0:n].astype(np.uint32) | (b[1:n+1].astype(np.uint32) << 8) | (b[2:n+2].astype(np.uint32) << 16) | (b[3:n+3].astype(np.uint32) << 24)).view(np.int32).astype(np.int64)
        tgt = img.tb + st + np.arange(n, dtype=np.int64) + 4 + d
        for t in targets:
            for i in np.nonzero(tgt == t)[0]: out[t].append(img.tb + st + int(i))
    return out
