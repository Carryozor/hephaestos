# Hephaestos

**Panneau de contrôle auto-hébergé pour serveurs de jeux dédiés Steam tournant sous Windows.**

Hephaestos pilote vos serveurs de jeux (Palworld, Valheim, et tout serveur dédié Steam) depuis une interface web unique : mise à jour, redémarrage, sauvegarde, gestion des mods Workshop, édition des fichiers de config, suivi des joueurs — sans jamais ouvrir de session RDP sur la machine Windows.

Le backend tourne sur une machine Linux (conteneur Docker). Un agent PowerShell léger tourne sur la machine Windows où sont installés les jeux et **tire** ses ordres depuis le backend (aucun port entrant à ouvrir côté Windows).

```
┌─────────────────────────┐         ┌──────────────────────────────┐
│  Backend (Linux/Docker) │         │  Machine Windows (les jeux)  │
│                         │         │                              │
│  FastAPI + UI web ──────┼─ HTTP ──┤  Agent PowerShell (pull 2min)│
│  état: data/state.json  │  sortant│    ├─ steamcmd (install/MAJ) │
│  :8710 (réseau privé)   │◄────────┤    ├─ tâches planifiées      │
│                         │  polling│    ├─ RCON / arrêt propre    │
└─────────────────────────┘         │    └─ sauvegardes            │
                                    └──────────────────────────────┘
```

## Fonctionnalités

- **Cycle de vie serveur** : démarrer / arrêter / redémarrer / mettre à jour, arrêt propre annoncé aux joueurs via RCON.
- **Mises à jour automatiques** : détecte un nouveau buildid Steam et met à jour quand le serveur est vide (0 joueur), avec temporisation anti-boucle.
- **Mods Workshop** (Palworld et jeux compatibles) : navigateur Workshop, installation/retrait, badge « mise à jour dispo ».
- **Sauvegardes** : automatiques avant chaque mise à jour + quotidiennes, restauration en un clic.
- **Édition des fichiers de config** du jeu depuis l'UI (avec anti-conflit et sauvegarde de secours).
- **Wizard de déploiement** : recherche d'un serveur dédié par nom, installation et configuration guidées.
- **Comptes multiples** : rôle admin (tout) ou user (contrôle limité aux serveurs assignés).
- **Supervision** : sondes de santé HTTP par serveur (compatibles Uptime Kuma).
- **Première configuration guidée** : création du compte administrateur directement depuis l'interface au premier lancement.

## Démarrage rapide (backend)

Prérequis : une machine Linux avec Docker et Docker Compose.

```bash
git clone <url-de-votre-fork> hephaestos
cd hephaestos/deploy

# 1. Configuration
cp .env.example .env
sed -i "s/REMPLACER_openssl_rand_hex_24/$(openssl rand -hex 24)/" .env   # jeton agent

# 2. Données initiales (montées dans le conteneur via ./data)
mkdir -p data
cp servers.json data/servers.json
cp known-dedicated-servers.json data/known-dedicated-servers.json

# 3. Build + démarrage
docker compose up -d --build

# 4. Vérifier
curl -s http://127.0.0.1:8710/api/health   # -> {"ok": true}
```

Ouvrez ensuite `http://127.0.0.1:8710/` dans un navigateur : au **tout premier accès**, Hephaestos affiche l'écran de **première configuration** pour créer le compte administrateur. Une fois créé, cet écran se verrouille définitivement.

> **Réseau.** Le `compose.yml` fourni publie le port sur `127.0.0.1` uniquement. Pour un accès distant, placez le service derrière un VPN (Tailscale, WireGuard…) ou un reverse-proxy HTTPS — n'exposez **jamais** le port `8710` directement sur Internet. Si vous passez en HTTPS, activez `HEPHAESTOS_COOKIE_SECURE=1`.

Guide détaillé : **[docs/backend-setup.md](docs/backend-setup.md)**.

## Agent Windows

Une fois le backend en ligne, installez l'agent sur la machine où tournent les jeux. Voir **[docs/agent-windows.md](docs/agent-windows.md)**.

## Supervision (optionnel)

Sondes de santé prêtes pour Uptime Kuma : **[docs/monitoring-kuma.md](docs/monitoring-kuma.md)**.

## Développement

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q          # suite backend (Python)
```

Portes de qualité (voir `check.sh`) : **ruff** (lint) + **mypy** (typage). L'agent PowerShell est testé avec **Pester** (voir `agent/tests/`).

## Architecture (repères)

| Élément | Emplacement |
|---|---|
| API + UI (FastAPI) | `app/` |
| État persistant | `data/state.json` (un seul fichier JSON, écriture atomique + fsync) |
| Agent Windows | `agent/hephaestos-agent.ps1` + `agent/hephaestos-lib.ps1` |
| Déploiement | `deploy/` (Dockerfile, compose, entrypoint) |

Le backend est **mono-process** par conception (caches et verrou d'état en mémoire) : ne pas lancer avec `workers=N` sans refonte.

## Licence

MIT — voir [LICENSE](LICENSE).
