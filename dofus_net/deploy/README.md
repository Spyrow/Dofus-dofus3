# Mettre l'interface du bot en ligne (VPS Linux, compte admin)

L'interface n'écoute que sur `127.0.0.1` ; c'est un proxy HTTPS (Caddy) ou un tunnel (Cloudflare) sur la même machine qui la montre au monde.
Dès que `DOFUS_ADMIN_PASSWORD_HASH` est défini, **toutes** les pages et appels d'API exigent la connexion admin (cookie signé, HttpOnly, Secure,
12 h, 5 essais ratés → blocage 5 min). Sans ce hash, le serveur refuse de démarrer avec `DOFUS_PUBLIC_HOST` ou une adresse non locale.

1. Serveur : `python3` 3.12, `pip install UnityPy` (seulement pour régénérer les données), utilisateur `dofus`, code dans `/opt/dofus_net`.
2. Données (dossier `%APPDATA%\dofus_net` du PC → `/var/lib/dofus_net`, via `scp -r`) : `gamedata.json`, `questdata.json`, `mapcells/`, `mapindex.json`,
   `mapgraph.json`, `recipes.json`, `skills.json`, `achievements.json`, `accounts.json`, `tokens.json`, `cert*.json`. Ce sont des **jetons de vos comptes** :
   `chmod -R go-rwx /var/lib/dofus_net`. Les cartes viennent des fichiers du jeu : si une carte manque, copier aussi le dossier du jeu ou la régénérer sur le PC.
3. Ajout d'un compte Dofus : la connexion (PKCE) ouvre un navigateur et un rappel sur le port 9001, donc **à faire sur le PC**, puis recopier `accounts.json`.
4. `python3 /opt/dofus_net/client/webui/auth.py hash` → coller la ligne dans `/etc/dofus-webui.env` (modèle : `dofus-webui.env.example`).
5. `systemctl enable --now dofus-webui`, puis Caddy (`Caddyfile.example`) ou le tunnel. Ouvrir `https://<DOFUS_PUBLIC_HOST>/` → page de connexion.

Vercel / hébergement « serverless » ne convient pas : le bot garde des connexions de jeu ouvertes en continu.
Rappel : automatiser le client viole les conditions d'Ankama (risque de bannissement des comptes).
