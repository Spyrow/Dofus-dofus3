"""Dofus 3 game wire format: varint length prefix + protobuf; payloads wrapped in google.protobuf.Any
(type_url = 'type.ankama.com/<obfuscated class name>', names change every client build)."""
import socket, threading

def enc_varint(n):
    n &= (1 << 64) - 1; out = bytearray()
    while True:
        b = n & 0x7f; n >>= 7
        if n: out.append(b | 0x80)
        else: out.append(b); return bytes(out)

def dec_varint(b, i=0):
    r = s = 0
    while True:
        c = b[i]; i += 1; r |= (c & 0x7f) << s; s += 7
        if not c & 0x80: return r, i

def f_varint(f, v): return enc_varint(f << 3) + enc_varint(v)
def f_bytes(f, v):
    if isinstance(v, str): v = v.encode()
    return enc_varint(f << 3 | 2) + enc_varint(len(v)) + v

def any_msg(type_name, body=b""):
    return f_bytes(1, "type.ankama.com/" + type_name) + (f_bytes(2, body) if body else b"")

def request(type_name, body=b"", req_id=-1):
    """Client->server envelope: field 4 = {1: request id (-1 = no reply expected), 2: Any}."""
    return f_bytes(4, f_varint(1, req_id) + f_bytes(2, any_msg(type_name, body)))

def parse(b):
    out, i = [], 0
    while i < len(b):
        k, i = dec_varint(b, i); f, w = k >> 3, k & 7
        if w == 0: v, i = dec_varint(b, i)
        elif w == 2:
            n, i = dec_varint(b, i); v = b[i:i+n]; i += n
        elif w == 1: v = b[i:i+8]; i += 8
        elif w == 5: v = b[i:i+4]; i += 4
        else: raise ValueError("bad wire type")
        out.append((f, v))
    return out

class GameConn:
    def __init__(self, host, port=5555):
        self.s = socket.create_connection((host, port), timeout=15); self.buf = b""; self._wlock = threading.Lock()
    def send(self, payload):
        with self._wlock: self.s.sendall(enc_varint(len(payload)) + payload)   # callable from several threads
    def recv_frame(self):
        while True:
            if self.buf:
                try:
                    n, i = dec_varint(self.buf)
                    if len(self.buf) >= i + n:
                        fr, self.buf = self.buf[i:i+n], self.buf[i+n:]; return fr
                except IndexError: pass
            d = self.s.recv(65536)
            if not d: raise ConnectionError("closed")
            self.buf += d
    def recv_message(self):
        """-> (type_name, body_bytes) for a server push {3: {1: Any}}."""
        for f, v in parse(self.recv_frame()):
            if f == 3:
                for f2, any_ in parse(v):
                    if f2 == 1:
                        d = dict(parse(any_)); return d[1].decode().split("/", 1)[1], d.get(2, b"")
        return None, b""
