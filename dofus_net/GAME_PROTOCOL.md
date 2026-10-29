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
`ipe` métiers · `ism` barre de raccourcis = sorts du perso (ids de la table `spells`) · `ksc` contient aussi la classe (`2.4.5.3`, table `breeds`).
`jow` (carte courante) n'arrive que si le client envoie d'abord, après `izn` : `lnt{1:1} kne{2:1} kkn kkt klu klx kmj jgm{1:3} jgm jcy kkc` (`client/login.py` READY) ; il acquitte `jow` par `kjt{id}` + `jpe{id}`.
Le panneau de succès fait `mdm{onglet}` -> `mdo` (liste de succès) : ce ne sont pas des sorts.

## Déplacement et changement de carte (une capture, zone Incarnam — à confirmer)
- `jok {1: chemin, 2: id carte}` : chemin = varints packés `(direction << 12) | case`, seulement les cases de changement de direction ;
  le serveur répond `jnz {1: chemin complet, 2: ?, 4: id acteur}`.
- Arrivé : client `jny` (vide, fin de déplacement). Sortie par le bord : client `joc {2: id carte voisine - 512}`.
- Serveur `jow {1: nouvelle carte}` ; client `kjt {1: id}` + `jpe {1: id}` (carte chargée) ; serveur `jnj` (acteurs de la carte).
- Autre sortie (porte/objet de la carte) : client `itz {1: ?, 3: ?}` au lieu de `joc` -> `jow`.
- `kne {2: 1}` toutes les ~5 s = keep-alive.
- Voisines : table `mapscrollactions` (right/bottom/left/top) ; coordonnées/nom : `mapsinformation` ; exportées par `tools/export_gamedata.py`.

## Déplacement : cases, chemin, changement de carte (implémenté dans `client/gamemaps.py` + `gamesession.py`, vérifié en jeu)
- Cartes : `Content/Map/Data/mapdata_assets_world_*.bundle`, un MonoBehaviour `map_<id>` : 560 cases (14 x 20, lignes entrelacées),
  `cellsData[i].mov` = franchissable, `.mapChangeData` = masque des sorties (1 E, 2 SE, 4 S, 8 SO, 16 O, 32 NO, 64 N, 128 NE ; droite/bas/gauche/haut = 1/4/16/64),
  `right/bottom/left/topNeighbourId`. Index id -> bundle : `tools/index_maps.py`.
- Géométrie : case i -> p = i // 28, r = i % 28, c = r % 14 ; x = p + c + (r >= 14), y = c - p. Directions 0..7 = pas (+1,+1) (+1,0) (+1,-1) (0,-1) (-1,-1) (-1,0) (-1,+1) (0,+1).
  Clé de chemin = `(direction << 12) | case`, une clé au départ, à chaque changement de direction, et à l'arrivée (direction du dernier segment).
- Marche : `jok {1: clés packées, 2: id carte}` -> `jnz` (chemin complet) -> attendre ~0,14 s/case -> `jny`.
- Changement de carte par le bord : marcher jusqu'à une case dont `mapChangeData` contient le bit du côté, `jny`, puis `joc {2: id voisin du bundle}` -> `jow` (id de la nouvelle carte, format différent de celui de `joc`).
  Arrivée sur une case de bord : `joc` peut être renvoyé sans marcher (test : aller-retour Astrub 5,-17 <-> 6,-17).

## Données statiques (`tools/export_gamedata.py`)
Bundles Unity `Dofus_Data/StreamingAssets/Content/Data/*.asset.bundle` + textes `Content/I18n/<lang>.bin` (index u32 id/offset, chaînes varint+UTF-8).
