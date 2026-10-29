"""Minimal Thrift (binary, strict) client for the Ankama launcher (Zaap) local API."""
import socket, struct

class Zaap:
    def __init__(self, port, game="dofus", release="dofus3", instance_id=1, host="127.0.0.1"):
        self.s = socket.create_connection((host, port), timeout=10)
        self.seq = 0
        self.game, self.release, self.instance = game, release, instance_id

    @staticmethod
    def _str(fid, v):
        b = v.encode(); return struct.pack(">BhI", 11, fid, len(b)) + b
    @staticmethod
    def _i32(fid, v): return struct.pack(">Bhi", 8, fid, v)

    def _recvn(self, n):
        buf = b""
        while len(buf) < n:
            c = self.s.recv(n - len(buf))
            if not c: raise ConnectionError("closed")
            buf += c
        return buf

    def call(self, name, *fields):
        nb = name.encode()
        self.s.sendall(struct.pack(">II", 0x80010001, len(nb)) + nb
                       + struct.pack(">i", self.seq) + b"".join(fields) + b"\x00")
        self.seq += 1
        ver, = struct.unpack(">I", self._recvn(4))
        if ver >> 16 != 0x8001: raise ValueError("bad thrift reply")
        n, = struct.unpack(">I", self._recvn(4)); rname = self._recvn(n).decode()
        self._recvn(4)  # seqid
        return rname, self._read_struct()

    def _read_struct(self):
        out = {}
        while True:
            t, = self._recvn(1)
            if t == 0: return out
            fid, = struct.unpack(">h", self._recvn(2))
            if t == 11:
                n, = struct.unpack(">I", self._recvn(4)); out[fid] = self._recvn(n).decode(errors="replace")
            elif t == 8: out[fid], = struct.unpack(">i", self._recvn(4))
            elif t == 2: out[fid] = bool(self._recvn(1)[0])
            elif t == 12: out[fid] = self._read_struct()
            else: raise ValueError(f"unhandled thrift type {t}")

    def connect(self, hash_):
        return self.call("connect", self._str(1, self.game), self._str(2, self.release),
                         self._i32(3, self.instance), self._str(4, hash_))

    def game_token(self, hash_, game_id=1):
        _, r = self.call("auth_getGameToken", self._str(1, hash_), self._i32(2, game_id))
        return r.get(0)
