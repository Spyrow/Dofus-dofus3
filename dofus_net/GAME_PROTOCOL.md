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

## Quêtes et dialogues (implémenté dans `client/gamequests.py` + `gamesession.py`, lecture vérifiée en jeu)
- Connexion : `ica {5: quêtes terminées, 6: quêtes actives}` puis un `icj` par quête active. `icj {1: {1: id quête, 3: {1: id étape, 3: [{1: 1 = en cours (absent = terminé), 4: id objectif}]}}}`.
  Mises à jour : `icb {id quête}`, `ibg {1: id objectif, 2: id quête}` (objectif accompli).
- Données statiques (`questdata.json` via `tools/export_gamedata.py`) : quêtes -> étapes -> objectifs ; types d'objectif (1 « Aller voir #1 », 6/14 « Vaincre x#2 #1 », 3 « Ramener »…).
- Un PNJ de la carte est un acteur à id négatif ; son modèle est en `3.1.4.7` de l'entrée `jnj`/`jns` (-> table `npcs`).
- Dialogue : `ilt {1: 3 (= Parler, table npcactions), 2: id carte, 3: id acteur PNJ}` -> `ilx {3: carte, 5: acteur}` + `ilf {1: [{1: id réplique}], 3: id message PNJ}` ;
  réponse du joueur : `iky {1: id réplique}` -> nouvel `ilf`, ou `kis` (fin) + `icb`/`icj`. Textes : `npcs[].dialogMessages / dialogReplies` = paires `[id, id i18n]`.
  Un `ilm` vide en réponse à `ilt` = refus (PNJ sans dialogue pour l'état de quête courant).
- Succès : `mdw {-1}` réclame les récompenses (`mdj {id}` par succès obtenu).

## Autopilote de quêtes (`client/autopilot.py`)
- Objectifs gérés : types 1 / 9 (« Aller voir / Retourner voir #1 » : voyage -> parler -> répondre). Voyage = BFS sur `mapgraph.json` (voisines des bundles), un `joc` par carte.
- Non gérés (arrêt + message) : combats, fabrication, remise d'objets, découverte, et tout passage par un élément interactif (porte, zaap) :
  `itz {1: ?, 3: id élément}` -> `jow`. Les éléments existent dans les bundles de carte (`references[].data.<interactiveId>k__BackingField`, avec une position en pixels)
  mais la carte de destination est décidée par le serveur (non présente dans les données) : les zones du tutoriel, Incarnam et Astrub ne sont donc pas reliées dans le graphe.

## Portes / éléments interactifs (`client/gamedoors.py`, `tools/learn_doors.py`)
- `jnj` liste les éléments interactifs de la carte (champ 7) : `{4: id du modèle d'élément (table `interactives`, ex. 475 = « Portes du Temple Céleste », -1 = inconnu), 5: {1: id compétence, 3: type (184 pour les portes vues)}, 6: id élément}`. La case de l'élément n'y est PAS (5.2 n'est pas une case).
  Utiliser un élément : `itz {1: id compétence, 3: id élément}` -> `jow` (nouvelle carte). La destination est décidée par le serveur : on l'apprend en observant un passage
  (`learn_doors.py` sur une capture, ou en utilisant l'élément depuis l'onglet Map). Base : `%APPDATA%/dofus_net/doors.json`, utilisée par le BFS de l'autopilote.
- Le vrai client marche d'abord jusqu'à côté de la porte (`jok`, `jny`) puis envoie `itz`. Envoyer `itz` sans être à côté (cas testé sur « Champs ») est ignoré en silence. La case de la porte n'est ni dans `jnj` ni déductible de la position des bundles (calage pixels -> cases impossible : l'axe vertical n'est pas une coordonnée de sol).

## Fin de dialogue et suivi de quête
- Après un dialogue qui modifie une quête, le serveur envoie `icb {id quête}` ; le vrai client répond `ibv {3: id quête, 4: -1}` et le serveur renvoie les `icj` à jour
  (implémenté dans `gamesession.py`). Une quête terminée disparaît de la liste (la suivante apparaît par `icj`).
- Porte utilisable : il faut se tenir sur la case où le vrai client se tenait (`stand` dans `doors.json`, relevée par `tools/learn_doors.py`).
- Test réel : autopilote Champs -> porte 489349 -> tour du Capitaine -> dialogue en 5 réponses -> quête « Mise à l'épreuve » terminée, « Champs de bataille » commencée.

## Combat (`client/fightbot.py`, première version ; connexion/placement vérifiés en jeu, tours NON encore vérifiés)
- Attaquer un groupe de la carte : marcher à côté, `hms {2: id acteur du groupe}` -> `jow` (carte de combat) + `jxi` (placement) ; client `igk`, puis `jxj {1:1}` (prêt).
  Un groupe est un acteur à id négatif dont `3.1.5.6` contient les monstres (champ 4 = meneur, champ 3 répété = les autres ; `5` = id monstre, `4` = niveau).
- Mon tour : `jvi`, `jti`, `jvj {2: durée, 3: acteur}` puis `jte` (vide). Actions : `jsr {1: sort, 2: case}`, `jok` (déplacement, sans `jny`), `jwo` (fin de tour).
  Le coût en PA se lit dans les tables de sorts ; l'effet dans `jtk` (variation de caractéristique) et `jsq`.
- Acquittements : tout `jug {2: acteur}` appelle `jtd {1:1}` ; un `jsy {2: id, 4: acteur}` de MON acteur dont l'id est dans {3,7,8,10,12,13,15,16,17,23,59} appelle `jst {2: id, 3: 1}`
  (règle déduite de deux captures, 14/14 pour les ids 3 et 8, 5/5 pour 12 et 16...). Ne pas acquitter les autres.
- Fin : `jwp` / `jwh` (résultats) puis la carte d'exploration est renvoyée (`jow`). La mort d'un ennemi n'a pas de message identifié.
- Attention : couper la connexion en plein combat laisse le personnage retenu côté serveur : la reconnexion reste muette quelques minutes.

## Connaissances partagées (`dofus_net/knowledge/`, versionné, sans données personnelles)
- `questbook.json` (`client/questbook.py`) : quêtes vues (étapes, objectifs), objectifs résolus (dialogue ou combat) et chaque issue de dialogue
  (chemin de réponses, objectifs validés dans les 6 s, impasses). L'autopilote suit un chemin qui a déjà validé un objectif actif, sinon évite les impasses connues.
- `doors.json` (`client/gamedoors.py`) : éléments interactifs et cases-déclencheurs appris. Alimentés en direct par les sessions et par `tools/learn_quests.py` / `tools/learn_doors.py` sur des captures.

## Données statiques (`tools/export_gamedata.py`)
Bundles Unity `Dofus_Data/StreamingAssets/Content/Data/*.asset.bundle` + textes `Content/I18n/<lang>.bin` (index u32 id/offset, chaînes varint+UTF-8).
