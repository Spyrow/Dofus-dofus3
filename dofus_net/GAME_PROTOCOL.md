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
  `right/bottom/left/topNeighbourId`. Une bordure est marquée soit par son bit central seul, soit (cartes en coin) par les deux bits diagonaux qui l'encadrent : bas = 2+8 (flag 10), droite = 2+128 (flag 130), gauche = 8+32, haut = 32+128 ; un bit diagonal isolé = case de coin, ambiguë, ignorée. Index id -> bundle : `tools/index_maps.py`.
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
- **Déplacement en combat : uniquement en diagonale** (directions impaires 1, 3, 5, 7, c'est-à-dire pas de ligne droite horizontale/verticale). Un pas direction 0 ou 6 est refusé par `jpc {2: compteur, 3: -7}` ; en exploration les 8 directions restent permises. `gamemaps.find_path(..., diagonals_only=True)`.
- **Déplacements forcés** : `jsq {22: 5, 5: {1: acteur, 2: case précédente, 3: nouvelle case}}` (glissement / charge, ex. le Bouftor Éthéré se colle au joueur). À suivre, sinon on vise une case périmée. Réponses d'erreur : `lof {2: id, 3: 1}` + `jpc` ; textes dans la table `infomessages` (type 1) : 174 « un obstacle gêne votre vue », 276 « un obstacle bloque le passage » (un combattant sur la ligne ou la case). Les téléportations / échanges de place (autres actions `jsq`) ne sont pas encore suivis.
- Acquittements : tout `jug {2: acteur}` appelle `jtd {1:1}` ; un `jsy {2: id, 4: acteur}` de MON acteur dont l'id est dans {3,7,8,10,12,13,15,16,17,23,59} appelle `jst {2: id, 3: 1}`
  (règle déduite de deux captures, 14/14 pour les ids 3 et 8, 5/5 pour 12 et 16...). Ne pas acquitter les autres.
- Fin : `jwp` / `jwh` (résultats) puis la carte d'exploration est renvoyée (`jow`). La mort d'un ennemi n'a pas de message identifié.
- Attention : couper la connexion en plein combat laisse le personnage retenu côté serveur : la reconnexion reste muette quelques minutes.

## Connaissances partagées (`dofus_net/knowledge/`, versionné, sans données personnelles)
- `questbook.json` (`client/questbook.py`) : quêtes vues (étapes, objectifs), objectifs résolus (dialogue ou combat) et chaque issue de dialogue
  (chemin de réponses, objectifs validés dans les 6 s, impasses). L'autopilote suit un chemin qui a déjà validé un objectif actif, sinon évite les impasses connues.
- `doors.json` (`client/gamedoors.py`) : éléments interactifs et cases-déclencheurs appris. Alimentés en direct par les sessions et par `tools/learn_quests.py` / `tools/learn_doors.py` sur des captures.

## Guide IA (Groq) (`client/groq.py`, `client/guide.py`)
- Clé : variable `GROQ_API_KEY` ou `%APPDATA%/dofus_net/groq.json` (hors dépôt ; l'UI peut l'enregistrer, jamais relue). Modèle par défaut `llama-3.3-70b-versatile` (`GROQ_MODEL` / `groq.json`).
- Rôles : (1) réponses de dialogue inconnues du livre de quêtes, (2) action suivante quand l'autopilote ne trouve aucun objectif gérable, (3) instruction libre du joueur traduite en actions.
- Le modèle propose, le bot valide : actions autorisées `talk, change_map, use_element, go_map, attack, stop`, paramètres vérifiés contre l'état réel (PNJ, éléments, sorties, groupes ≤ 3, cartes avec route) ; tout le reste est ignoré. Erreur/429 -> repli sur le livre puis l'heuristique.
- Envoyé à Groq : textes de quêtes/dialogues, noms de cartes/PNJ/éléments. Jamais de jeton, e-mail, nom de compte ou de personnage.
- Fin de quête : le serveur n'envoie rien ; `ibj {quête}` est répondu par `icj` tant que la quête est active, une quête muette est terminée (`Session.sync_quests`).

## Points de caractéristiques (`client/boost.py`)
- `krc {N: nombre de boosts}` dépense des points de capital (caractéristique 3, `statsPoints`) ; capture : `krc {4: 40}` -> Chance (13) +40, points 40 -> 0, prospection +4 ; réponse `krg` + `iqq`.
  Seul le champ 4 = Chance est observé ; les autres suivent l'hypothèse `champ = id - 9` (10 Force, 11 Vitalité, 12 Sagesse, 13 Chance, 14 Agilité, 15 Intelligence), contrôlée au premier envoi de chaque caractéristique (1 boost, lecture du `krg`)
  et corrigée automatiquement (`knowledge/boost_fields.json`). Coût d'un boost : table `breed_costs` (classe x caractéristique x paliers de valeur).
- Réglages par compte : `%APPDATA%/dofus_net/boost.json` (poids en %, répartition automatique à la connexion / montée de niveau). UI : onglet Fiche, carte « Boost de caractéristiques ».

## Données statiques (`tools/export_gamedata.py`)
Bundles Unity `Dofus_Data/StreamingAssets/Content/Data/*.asset.bundle` + textes `Content/I18n/<lang>.bin` (index u32 id/offset, chaînes varint+UTF-8).

## Métiers : récolte, fabrication, XP (`client/gamejobs.py`, capture du 2026-10-30)
- **Récolte** : marcher à côté de la ressource (sa case = `cellId` de l'élément dans le bundle de carte, = `itw.4`), puis `itz {1: compétence, 3: élément}`.
  Réponses : `itw {1: {3: élément, 4: case, 5: 2}}`, `itx`, `itv {1: élément, 4: acteur, 5: durée}` ; environ 3 s plus tard `itt {1: élément}` (fin),
  `isz {2: quantité, 3: objet}` (butin), `irx {3: {1: nouvelle quantité de la pile, 3: uid}}`, `ipz` (XP), `itw {.. 5: 1}`. Un élément indisponible ne répond rien.
- **Fabrication** : `itz` sur l'atelier (depuis la bonne case, comme une porte) -> `jyz {1: compétence de la recette}` + `irl` ; `kar {1: objet à fabriquer}` -> `kch`
  (le serveur pose les ingrédients) ; `kba {1: 1, 2: 1}` -> `kco {2: {4: objet créé}}`, `irx`, `ipz`, `ipa` ; fermer l'interface = `kiq` x2 -> `kbr`.
  La compétence de la recette est `skillId` de `data_assets_recipesdataroot.asset.bundle` (23 potion, 135 poisson, 27 pain).
- **XP des métiers** : `ipz {1: {1: source (2 récolte, 3 fabrication), 2: XP, 3: XP au début du niveau, 5: id métier, 6: XP du niveau suivant}}` ;
  niveau n+1 commence à 10·n·(n+1) XP (20, 60, 120...). L'XP et le niveau s'affichent dans l'onglet Métiers.
- Objectif de quête type 17 « Fabriquer #2 #1 et fermer l'interface » : `p = [objet, quantité, 0]`, `map` = carte de l'atelier. L'autopilote récolte ce qui manque
  (ressource = élément dont le nom est celui de l'ingrédient), va à l'atelier du métier (nom « Atelier des <métier> ») et fabrique.
- Non vérifié en direct : `kba` (le sens exact de ses deux champs), la récolte du Blé (même mécanisme, compétence lue dans `jnj`), l'atelier du Pain.

## Déplacements : relief, rampes (`gamemaps.can_step`) — vérifié sur ~330 pas du vrai client et sur les refus du serveur
- Les cases ont `altitude`, `linkedZone`, `moveZone` en plus de `mov / los / mapChangeData` (le cache `mapcells/*.json` garde 6 valeurs par case).
- L'altitude ne change que le long d'une rampe : un pas entre deux altitudes différentes n'est permis que si les DEUX cases ont `moveZone = 1`.
  Sinon le serveur répond `jpc {2: compteur, 3: code}` (codes vus : -11, 2) et ignore tout le trajet. Couper un coin en diagonale est permis.
- Un trajet refusé est rejoué case par case (`Session._walk_stepwise`) ; une case refusée est mémorisée dans `knowledge/bad_cells.json`.
- Après l'examen d'un objet qui ouvre une interface (longue-vue), le premier déplacement est avalé : envoyer `kiq` avant.

## Éléments : compétences (`data_assets_skillsdataroot.asset.bundle`, `gamejobs.skills()`)
- Le `type` d'un élément `jnj` est un id de compétence : `gatheredRessourceItem` = objet récolté (Blé 289 <- types 45 / 296), `craftableItemIds` = objets
  fabriqués (four 27 -> Pain d'Incarnam 468), `range` = distance d'utilisation (1 = voisin direct dans la grille de jeu ; 10 pour la pêche).
- Les éléments statiques (portes, ateliers) n'ont pas de case dans la carte, seulement une position en pixels ; seuls les éléments animés ont un `cellId`
  (case = (x + 623,5 ; 505,25 - y) px). La case d'utilisation se prédit mal (~13 cas sur 19 dans les 3 premières) : on mémorise celles qui ont marché.

## Combat contre un boss (`fightbot._kite_turn`)
- `juo` / `jwj` donnent pour chaque combattant le modèle (3.4.6.3.3), la vie (3.4.7.4, stat sans id) et les PM (stat 23) : Kardorim 140 PV / 4 PM, chafer 54 PV / 3 PM.
- Tactique (Sacrieur) quand il y a 4 ennemis ou plus : Mutilation une fois par combat, Stase (1-5 cases) sur le boss ou l'ennemi le plus proche à portée,
  puis fuite avec les PM restants vers la case la moins menacée. Kardorim vaincu ainsi au premier essai.

## Équipement, succès, offres de quête, achats (captures du 2026-10-30)
- **Équiper** : `ito {1: 1, 2: uid, 3: emplacement}` (1 = arme / outil, 63 = dans le sac) -> `iqn {1: uid, 3: emplacement}`. Les `uid` changent à chaque connexion.
- **Succès** : `mdw {1: -1}` réclame toutes les récompenses en attente -> `mdj {1: id du succès, 3: 1}` par succès (récompenses : `krg`, `iqx`, `ith`, `kaq`).
  Les noms viennent de `data_assets_achievementsdataroot.asset.bundle` + `I18n/fr.bin` (`client/gameachievements.py`). `mdp` (login du vrai client) liste les succès débloqués.
- **Offre de quête portée par un élément** (panneau d'offres d'emploi) : `itz` sur l'élément, puis `icf {1: id de la quête}` accepte, `kiq` ferme (`knowledge/quest_starters.json`).
- **Achat à un PNJ** : un simple dialogue (`ilt`, `iky`) dont une réponse nomme l'objet (« Prendre un lailait bien frais pour 4 kamas ») ; l'objet arrive par `iqx`.
- **Relief et zones** : cf. `gamemaps.can_step`. **Cases de ressources** : `jnj` champ 2 `{3: élément, 4: case}` (portes et ateliers n'y figurent pas).
- **Guides scrapés** (`knowledge/dpln_quests.json`) : le bot y lit le PNJ de départ, les monstres qui lâchent un objet (« se drop sur le Tofu Chimérique ») et les vendeurs.
