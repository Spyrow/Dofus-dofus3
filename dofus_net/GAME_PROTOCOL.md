# Protocole de jeu Dofus 3 (build 3.7.12.12) — observations

Noms de messages obfusqués, valables pour ce build uniquement : les re-dériver avec `tools/scan_connect.py` après chaque mise à jour.
Trame : varint de longueur + protobuf ; client→serveur `{4: {1: id requête (-1 = pas de réponse), 2: Any}}`, serveur→client `{3: {1: Any}}`,
`Any = {1: "type.ankama.com/<nom>", 2: corps}`.

## Connexion (implémentée dans `client/login.py`)
1. Serveur de connexion `dofus2-co-production:5555`, protobuf nu : jeton de jeu + hash Shield (sha256 majuscules) + version -> compte ;
   sélection du serveur (id 291) -> ticket de 32 car. + hôte du serveur de jeu.
2. Serveur de jeu : `knc(ticket, "fr")` -> `... kog, meq` -> `ksj` -> `... ksc, jpx` -> `kod` (DH, requête id 2) -> `kta(id perso)`.

## État poussé après `kta` (décodé dans `client/gamestate.py`)
`ksc` perso (id, nom) · `krg` caractéristiques (ids = CharacteristicData) · `irl`/`iqx` inventaire (position 63 = non équipé) ·
`ipe` métiers · `jow` carte courante. Les sorts n'ont pas été trouvés (perso niveau 1, panneau jamais ouvert dans la capture).

## Déplacement et changement de carte (une capture, zone Incarnam — à confirmer)
- `jok {1: chemin, 2: id carte}` : chemin = varints packés `(direction << 12) | case`, seulement les cases de changement de direction ;
  le serveur répond `jnz {1: chemin complet, 2: ?, 4: id acteur}`.
- Arrivé : client `jny` (vide, fin de déplacement). Sortie par le bord : client `joc {2: id carte voisine - 512}`.
- Serveur `jow {1: nouvelle carte}` ; client `kjt {1: id}` + `jpe {1: id}` (carte chargée) ; serveur `jnj` (acteurs de la carte).
- Autre sortie (porte/objet de la carte) : client `itz {1: ?, 3: ?}` au lieu de `joc` -> `jow`.
- `kne {2: 1}` toutes les ~5 s = keep-alive.
- Voisines : table `mapscrollactions` (right/bottom/left/top) ; coordonnées/nom : `mapsinformation` ; exportées par `tools/export_gamedata.py`.

## Données statiques (`tools/export_gamedata.py`)
Bundles Unity `Dofus_Data/StreamingAssets/Content/Data/*.asset.bundle` + textes `Content/I18n/<lang>.bin` (index u32 id/offset, chaînes varint+UTF-8).
