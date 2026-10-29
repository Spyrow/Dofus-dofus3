# Protocole local Zaap ↔ Dofus (observé)

Notes d'observation d'une capture loopback faite le 2026-10-29 pendant un lancement de Dofus 3 via l'Ankama Launcher. Aucune valeur de session, de hash, de ticket ni de données de compte n'est consignée ici.

## Contexte

Le launcher (Zaap) démarre `Dofus.exe` avec des arguments de ligne de commande (voir `zaap.yml`) :

| Argument | Rôle supposé |
|---|---|
| `--port` | port du service local de Zaap |
| `--gameName`, `--gameRelease` | jeu et release (ici `dofus` / `dofus3`, cf. `.release.infos.json`) |
| `--instanceId` | identifiant d'instance |
| `--hash` | hash d'instance, fourni par Zaap |
| `--canLogin` | autorisation de connexion |
| `--langCode`, `--autoConnectType`, `--connectionPort` | réglages du jeu |

Le jeu ne reçoit donc pas son ticket depuis Internet : il le demande à Zaap sur la boucle locale.

## Transport

- TCP sur `127.0.0.1:26116` (port observé du service Zaap).
- **Aucun chiffrement** (pas de TLS), donc ni proxy HTTPS, ni CA, ni pinning à considérer sur ce canal.
- Sérialisation **Apache Thrift, protocole binaire** : chaque message commence par `80 01 00 0T` (`T` = 1 appel, 2 réponse), suivi de la longueur du nom de méthode, du nom, d'un identifiant de séquence, puis d'une structure de champs numérotés.

## Séquence observée

| # | Méthode | Arguments (n° : type) | Réponse |
|---|---|---|---|
| 1 | `connect` | 1 : chaîne (5 car.) ; 2 : chaîne (6 car.) ; 3 : i32 (13) ; 4 : chaîne (36 car.) | chaîne de 36 car. (identifiant de session) |
| 2 | `settings_get` | 1 : session ; 2 : chaîne (15 car.) | chaîne (1 car.) |
| 3 | `userInfo_get` | 1 : session | chaîne JSON : profil du compte |
| 4 | `auth_getGameToken` | 1 : session ; 2 : i32 (1) | chaîne de 36 car. |
| 5 | `updater_isUpdateAvailable` | 1 : session | bool (false) |
| 6 | `auth_getGameToken` | 1 : session ; 2 : i32 (99) | chaîne de 36 car. |

Les clés du JSON de `userInfo_get` : `id`, `type`, `login`, `nickname`, `firstname`, `lastname`, `nicknameWithTag`, `tag`, `security`, `addedDate`, `locked`, `parentEmailStatus`, `avatar`, `isGuest`, …

## Points non établis

- Les noms des paramètres Thrift ne sont pas dans le flux ; la signification des champs de `connect` (jeu, release, constante 13) est une hypothèse.
- La signification des valeurs `1` et `99` du second argument de `auth_getGameToken` n'a pas été déterminée.
- La capture couvre un seul lancement : pas de reconnexion, d'expiration ni de cas d'erreur observés.

## Méthode de capture

1. Wireshark/Npcap installé, interface « Adapter for loopback traffic capture ».
2. `dumpcap -i <n° loopback> -f "tcp and host 127.0.0.1" -a duration:120 -w capture.pcapng`.
3. Lancer le jeu depuis le launcher pendant la capture.
4. Filtrer sur le port `26116` et décoder le Thrift binaire.

Les trois captures `.pcapng` déjà présentes à la racine du dossier (`dofus_5555`, `dofus_all`, `dofus_capture`) ne contiennent pas ce trafic : les deux premières sont vides, la troisième ne contient que le trafic d'outils de développement.

## Avertissement

Ce document décrit un canal interne au client officiel. Le réutiliser pour obtenir des tickets hors du jeu officiel (bots, clients tiers) contourne l'authentification d'Ankama et enfreint ses conditions d'utilisation.
