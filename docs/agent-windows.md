# Installation de l'agent Windows — pas à pas

L'agent est un script PowerShell qui tourne sur la machine Windows hébergeant les jeux. À chaque cycle (toutes les 2 minutes) il **tire** les ordres depuis le backend, exécute ce qu'il faut (steamcmd, tâches planifiées, RCON, sauvegardes) et **renvoie** l'état. Il n'ouvre aucun port entrant.

## 1. Prérequis

- **Windows** avec **PowerShell 5.1** (intégré) ou PowerShell 7+.
- **steamcmd** installé (par ex. `C:\steam\steamcmd.exe`) : https://developer.valvesoftware.com/wiki/SteamCMD
- Le backend Hephaestos joignable depuis cette machine (via VPN/réseau privé) — notez son URL, ex. `http://<ip-privée-backend>:8710`.
- Le **jeton agent** (`HEPHAESTOS_AGENT_TOKEN`) défini côté backend.

## 2. Copier l'agent

Créez le dossier `C:\hephaestos\` et copiez-y, depuis le dossier `agent/` du dépôt :

- `hephaestos-agent.ps1`
- `hephaestos-lib.ps1`
- `known-games.json`

```powershell
New-Item -ItemType Directory -Force -Path C:\hephaestos | Out-Null
# copiez les 3 fichiers ci-dessus dans C:\hephaestos\
```

## 3. Configurer l'agent

Copiez `agent/hephaestos-config.example.json` en `C:\hephaestos\hephaestos-config.json` et adaptez :

```json
{
  "api_base": "http://<ip-privée-backend>:8710",
  "agent_token": "<le même jeton que HEPHAESTOS_AGENT_TOKEN>",
  "steamcmd": "C:\\steam\\steamcmd.exe",
  "steamcmd_root": "C:\\steam",
  "backend_config_hash": "",
  "servers": [
    {
      "name": "palworld",
      "appid": 2394010,
      "process": "PalServer-Win64-Shipping-Cmd",
      "start_task": "PalServer",
      "stop_adapter": "palworld-rcon",
      "rcon": {"host": "127.0.0.1", "port": 25575}
    }
  ]
}
```

Champs par serveur (les clés doivent correspondre à celles du backend) :

| Champ | Rôle |
|---|---|
| `name` | Identifiant interne (identique à la clé dans `servers.json`). |
| `appid` | Appid Steam du serveur dédié. |
| `process` | Nom du processus Windows à surveiller. |
| `start_task` | Nom de la tâche planifiée qui lance le jeu (créée automatiquement, voir §5). |
| `stop_adapter` | Méthode d'arrêt : `palworld-rcon`, `generic-graceful`, `generic-force`, `rcon-generic`. |
| `rcon` | (Optionnel) hôte/port RCON pour l'arrêt annoncé et le comptage joueurs. |
| `kuma_maj_push` | (Optionnel) URL de push Uptime Kuma pour cet indicateur. |

> Une grande partie de cette configuration peut aussi être **poussée depuis le backend** (registre des serveurs) : renseignez le minimum ici, l'UI complète le reste.

## 4. Planifier l'agent (cycle toutes les 2 min)

Créez une tâche planifiée qui exécute l'agent toutes les 2 minutes, en compte **SYSTEM** :

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\hephaestos\hephaestos-agent.ps1"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Minutes 2)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "Hephaestos Agent" -Action $action -Trigger $trigger -Principal $principal
```

Vérifier :

```powershell
Start-ScheduledTask -TaskName "Hephaestos Agent"
Get-Content C:\hephaestos\hephaestos-agent.log -Tail 20
```

Côté backend, l'état du serveur doit apparaître dans l'UI dans les 2 minutes.

## 5. Tâches de démarrage des jeux

L'agent **crée lui-même** la tâche planifiée de démarrage de chaque jeu (déclencheur *au démarrage de Windows*, compte SYSTEM), nommée d'après `start_task`. Vous n'avez pas à la créer à la main : elle est posée au premier ordre `start`/`setup`. Un redémarrage de Windows relancera donc les serveurs configurés.

## 6. Journalisation et dépannage

- Log de l'agent : `C:\hephaestos\hephaestos-agent.log` (rotation à 1 Mo).
- « cycle demarre » / « cycle termine » à chaque passage = agent en bonne santé.
- Si l'état n'apparaît pas côté backend : vérifiez `api_base` (joignable depuis Windows ?), `agent_token` (identique des deux côtés ?), et le pare-feu sortant.

## 7. Mise à jour de l'agent

Recopiez `hephaestos-agent.ps1` et `hephaestos-lib.ps1` dans `C:\hephaestos\`. Le numéro `agent_version` remonté au backend confirme la version en place.
