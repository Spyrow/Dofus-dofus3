"""Reassemble Dofus 3 game TCP streams from a pcap and decode length-prefixed protobuf frames."""
import subprocess, sys, collections, json
TSHARK = r"C:\Program Files\Wireshark\tshark.exe"

def varint(b, i):
    r = s = 0
    while True:
        c = b[i]; i += 1
        r |= (c & 0x7f) << s; s += 7
        if not c & 0x80: return r, i

def parse(b, depth=0):
    """Generic protobuf decode -> list of (field, wiretype, value)."""
    out, i = [], 0
    while i < len(b):
        k, i = varint(b, i); f, w = k >> 3, k & 7
        if f == 0: raise ValueError
        if w == 0: v, i = varint(b, i)
        elif w == 1: v = b[i:i+8]; i += 8
        elif w == 5: v = b[i:i+4]; i += 4
        elif w == 2:
            n, i = varint(b, i); v = b[i:i+n]
            if len(v) != n: raise ValueError
            i += n
        else: raise ValueError
        out.append((f, w, v))
    return out

def render(b, depth=0):
    try:
        items = parse(b)
        if not items: raise ValueError
    except Exception:
        try:
            s = b.decode("utf-8")
            if s.isprintable(): return json.dumps(s)
        except Exception: pass
        return "0x" + b.hex()
    res = {}
    for f, w, v in items:
        if w == 0: v = v - (1 << 64) if v >= 1 << 63 else v
        elif w == 2: v = render(v, depth + 1)
        else: v = "0x" + v.hex()
        res.setdefault(f, []).append(v)
    return {k: (x[0] if len(x) == 1 else x) for k, x in res.items()}

def streams(pcap, ports):
    flt = " || ".join(f"tcp.port=={p}" for p in ports) + " && tcp.len>0"
    cmd = [TSHARK, "-r", pcap, "-Y", flt, "-T", "fields", "-e", "tcp.stream", "-e", "frame.time_relative",
           "-e", "ip.src", "-e", "tcp.srcport", "-e", "ip.dst", "-e", "tcp.dstport", "-e", "tcp.seq_raw", "-e", "tcp.payload"]
    st = collections.defaultdict(lambda: collections.defaultdict(dict))
    meta = {}
    for line in subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines():
        p = line.split("\t")
        if len(p) < 8 or not p[7]: continue
        sid, t, src, sp, dst, dp, seq, pl = p
        d = f"{src}:{sp}->{dst}:{dp}"
        st[sid][d][int(seq)] = (float(t), bytes.fromhex(pl.replace(":", "")))
        meta[sid] = (src, dst, dp)
    return st

def frames(buf_pkts):
    buf = b""; t0 = None; out = []
    for seq in sorted(buf_pkts):
        t, pl = buf_pkts[seq]
        if t0 is None: t0 = t
        buf += pl
        while buf:
            try: n, i = varint(buf, 0)
            except IndexError: break
            if len(buf) < i + n: break
            out.append((t, buf[i:i+n])); buf = buf[i+n:]
    return out

if __name__ == "__main__":
    pcap = sys.argv[1]; ports = [int(x) for x in sys.argv[2].split(",")]
    allf = []
    for sid, dirs in streams(pcap, ports).items():
        for d, pk in dirs.items():
            for t, fr in frames(pk): allf.append((t, sid, d, fr))
    for t, sid, d, fr in sorted(allf):
        print(f"{t:9.3f} s{sid} {d} [{len(fr)}] {json.dumps(render(fr), ensure_ascii=False)}")
