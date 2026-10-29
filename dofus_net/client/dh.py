"""Client side of the game's Diffie-Hellman step (enz.wzc): kod = G^a mod P as a decimal string.
Group: RFC 2409 768-bit (BouncyCastle DHStandardGroups.rfc2409_768), found by static analysis of GameAssembly.dll."""
import secrets
from dh_params import P, G

def make_kod():
    a = secrets.randbelow(P - 3) + 2
    return str(pow(G, a, P)), a
