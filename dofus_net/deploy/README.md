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

## Piloter depuis le téléphone, l'app restant sur le PC (Tailscale)
Aucun serveur à louer : `start_mobile.ps1` lance l'interface sur le PC et la publie en HTTPS **uniquement sur votre réseau privé Tailscale** (le téléphone doit y être connecté).
1. PC : `tailscale up` (connexion dans le navigateur). Console admin Tailscale → DNS : activer **MagicDNS** et **HTTPS Certificates**.
2. Téléphone : installer l'app Tailscale, se connecter au **même compte**.
3. PC : `powershell -File dofus_net\deploy\start_mobile.ps1` (première fois : choix du mot de passe admin). Garder la fenêtre ouverte.
4. Téléphone : ouvrir `https://<nom-du-pc>.<réseau>.ts.net/` (affiché par le script), se connecter (identifiant `admin`). « Ajouter à l'écran d'accueil » pour l'avoir comme une app.
Le PC doit rester allumé, le jeu et le bot tournent dessus. Le comptes se connectent depuis le PC (étape PKCE locale).
