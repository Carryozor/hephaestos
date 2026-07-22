# Installation du backend — pas à pas

Le backend Hephaestos est un conteneur Docker (FastAPI + interface web). Il détient l'état, sert l'UI, et expose l'API que l'agent Windows interroge.

## 1. Prérequis

- Une machine **Linux** avec **Docker** et le plugin **Docker Compose** (`docker compose version`).
- Un moyen d'accès privé au port de l'UI : VPN (Tailscale/WireGuard) ou reverse-proxy HTTPS. **Ne jamais exposer le port `8710` directement sur Internet.**
- (Optionnel) Une clé Web API Steam : https://steamcommunity.com/dev/apikey — requise uniquement pour les mods Workshop et la recherche de jeux.

## 2. Récupérer le code

```bash
git clone <url-de-votre-fork> hephaestos
cd hephaestos
```

## 3. Configurer l'environnement

Tout se passe dans le dossier `deploy/` :

```bash
cd deploy
cp .env.example .env
```

Éditez `.env` :

| Variable | Rôle |
|---|---|
| `COMPOSE_PROJECT_NAME` | Nom du projet Compose (isole volumes/réseaux). |
| `HEPHAESTOS_AGENT_TOKEN` | **Obligatoire.** Jeton partagé backend/agent. Générez-le : `openssl rand -hex 24`. |
| `STEAM_API_KEY` | Optionnel. Fonctions Workshop / recherche. |
| `HEPHAESTOS_ALERT_WEBHOOK` | Optionnel. Webhook (type Discord) sur ordre échoué/expiré. |
| `HEPHAESTOS_COOKIE_SECURE` | Mettre à `1` **uniquement** si l'UI est servie en HTTPS. |

Générer et injecter le jeton d'un coup :

```bash
sed -i "s/REMPLACER_openssl_rand_hex_24/$(openssl rand -hex 24)/" .env
```

> Gardez ce jeton : vous le recopierez dans la config de l'agent Windows.

## 4. Préparer les données

Le conteneur monte `./data` (dans `deploy/`) sur `/data`. Deux fichiers de référence y sont attendus ; copiez les modèles fournis :

```bash
mkdir -p data
cp servers.json data/servers.json                                   # liste de vos serveurs
cp known-dedicated-servers.json data/known-dedicated-servers.json   # catalogue pour la recherche
chown 1000:1000 data                                                # le conteneur tourne en UID 1000 (non-root)
```

`servers.json` décrit les serveurs que vous gérez. Exemple (adaptez à vos jeux) :

```json
{
  "palworld": {"display_name": "Palworld", "server_appid": 2394010, "workshop_appid": 1623730},
  "valheim":  {"display_name": "Valheim",  "server_appid": 896660}
}
```

- La clé (`palworld`) est l'identifiant interne.
- `server_appid` est l'appid Steam du **serveur dédié** (pas du jeu).
- `workshop_appid` (optionnel) active les mods Workshop pour ce serveur.

Vous pourrez aussi ajouter des serveurs plus tard depuis l'UI (wizard de déploiement).

## 5. Construire et démarrer

```bash
docker compose up -d --build
```

Vérifier que le service répond :

```bash
curl -s http://127.0.0.1:8710/api/health     # -> {"ok": true}
```

Consulter les logs si besoin :

```bash
docker compose logs -f
```

## 6. Première connexion — créer l'administrateur

Ouvrez `http://127.0.0.1:8710/` (via votre VPN/proxy si le backend est distant).

Au **premier accès**, aucun compte n'existe : Hephaestos affiche l'écran **« Première configuration »**. Saisissez un nom d'administrateur et un mot de passe (12 caractères minimum). Le compte est créé avec le rôle **admin** et vous êtes connecté immédiatement.

Dès qu'un compte existe, cet écran est **verrouillé** (l'API `POST /api/setup` renvoie 409) : plus personne ne peut s'auto-créer un accès. Les comptes suivants se gèrent depuis l'UI (bouton **Comptes**, réservé aux admins).

> **Alternative en ligne de commande** (si vous préférez ne pas utiliser l'écran web) :
> ```bash
> docker compose exec hephaestos python create-user.py <nom> <mot_de_passe>
> ```

## 7. Exposition réseau (rappel sécurité)

Le `compose.yml` publie `127.0.0.1:8710`. Pour un accès distant, choisissez **l'une** de ces options :

- **VPN** (recommandé) : rejoignez la machine à un tailnet Tailscale / réseau WireGuard, et changez le binding du port pour l'IP privée du VPN dans `compose.yml`.
- **Reverse-proxy HTTPS** (nginx, Caddy, Traefik) devant le port, avec `HEPHAESTOS_COOKIE_SECURE=1` dans `.env`.

L'API `/api/public/health/{serveur}` est volontairement sans authentification (destinée à une sonde de supervision) ; elle ne divulgue que l'état up/down d'un serveur. Gardez-la, elle aussi, sur le réseau privé.

## 8. Mise à jour du backend

```bash
git pull
cd deploy
docker compose up -d --build
```

L'état (`data/state.json`) et vos données sont préservés (volume `./data`). Pensez à sauvegarder ce fichier régulièrement : c'est la seule base de données du système.

## Étape suivante

Installer l'agent sur la machine Windows : **[agent-windows.md](agent-windows.md)**.
