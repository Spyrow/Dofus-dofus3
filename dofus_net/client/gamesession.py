"""Live session for one UI account: refresh the HAAPI key, get a game token, log in, enter the character, decode the
pushed state (gamestate.py) and write the account's snapshot. Needs the Shield certificate (shield.py) and the real
game closed (a second session on the same account kicks the first)."""
import accounts, shield, gamestate, login
from haapi import create_game_token, GAME_IDS


def fetch_snapshot(aid, idle=6):
    """Raises SystemExit(msg) on any expected failure (HAAPI error, no certificate, no character data)."""
    if not accounts.get(aid): raise SystemExit("compte inconnu")
    cert_id, cert_hash = shield.certificate_args()
    if not cert_hash: raise SystemExit("pas de certificat Shield : lance shield.py request puis shield.py validate <code>")
    accounts.refresh(aid)                                    # the saved key may have expired; rotates the refresh token
    key = accounts.get(aid)["key"]
    token = create_game_token(key, GAME_IDS["dofus"], cert_id, cert_hash)
    ticket, host = login.connection_step(token, cert_hash)
    msgs = []
    c = login.game_step(ticket, host, enter=True, idle=idle, collect=msgs, quiet=True, enter_map=True, max_seconds=25)
    c.s.close()
    snap = gamestate.snapshot(msgs, host)
    if not snap.get("characteristics"): raise SystemExit("le serveur n'a envoyé aucune caractéristique (personnage non sélectionné ?)")
    accounts.save_character(aid, snap)
    return snap
