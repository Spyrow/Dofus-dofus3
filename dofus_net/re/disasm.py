"""Disassemble an IL2CPP method and annotate calls / string literals / type refs using il2cpp.json names."""
import sys, json, bisect, re, pickle, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from index import *
from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import X86_OP_MEM, X86_REG_RIP

_A = r"C:\Users\alex\dofus_net\re\annot.pkl"
def load_annot():
    if not os.path.exists(_A):
        d = json.load(open(r"C:\Users\alex\dofus_meta\il2cpp.json", encoding="utf-8"))["addressMap"]
        ann = {}
        for x in d["stringLiterals"]: ann[int(x["virtualAddress"], 16)] = 'STR "%s"' % x["string"][:80].replace("\n", "\n")
        for x in d["typeInfoPointers"]: ann[int(x["virtualAddress"], 16)] = "TYPE " + x["dotNetType"]
        for x in d["methodInfoPointers"]: ann[int(x["virtualAddress"], 16)] = "METH " + x["dotNetSignature"]
        for x in d["typeRefPointers"]: ann[int(x["virtualAddress"], 16)] = "TREF " + x["dotNetType"]
        pickle.dump(ann, open(_A, "wb"))
    return pickle.load(open(_A, "rb"))

def short(n): return (n or "?").split("::")[0].split("/")[-1] + "::" + (n or "?::?").split("::", 1)[-1][:90]

def disasm(pattern, maxlen=6000, ann_only=False):
    m, _ = load(); addrs = [a for a, _ in m]; ann = load_annot(); img = Img()
    md = Cs(CS_ARCH_X86, CS_MODE_64); md.detail = True
    cands = [(a, n) for a, n in m if re.search(pattern, n)]
    for a, n in cands[:3]:
        i = bisect.bisect_right(addrs, a); end = addrs[i] if i < len(addrs) else a + maxlen
        end = min(end, a + maxlen); code = img.read(a, end - a)
        print(f"### {short(n)}  {hex(a)}..{hex(end)}")
        for ins in md.disasm(code, a):
            note = ""
            if ins.mnemonic in ("call", "jmp") and ins.operands and ins.operands[0].type == 2:
                t = ins.operands[0].imm; nm, off = name_of(m, addrs, t)
                if ins.mnemonic == "call" or nm != short(n): note = "-> " + (short(nm) if off == 0 else f"{short(nm)}+{off:#x}")
            for op in ins.operands:
                if op.type == X86_OP_MEM and op.mem.base == X86_REG_RIP:
                    t = ins.address + ins.size + op.mem.disp; note = ann.get(t, "") or note
            if ann_only and not note: continue
            print(f"  {ins.address:#x}: {ins.mnemonic:6} {ins.op_str:40} {note}")

if __name__ == "__main__":
    disasm(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 6000, ann_only=len(sys.argv) > 3)
