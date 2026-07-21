# Supervision avec Uptime Kuma (optionnel)

Hephaestos expose deux mécanismes de supervision complémentaires.

## 1. Sondes de santé HTTP (pull)

Sans authentification (destinées à une sonde interne, à garder sur le réseau privé) :

| Endpoint | Signification |
|---|---|
| `GET /api/health` | Le backend est vivant (`{"ok": true}`). |
| `GET /api/public/health/{serveur}` | `200` si le serveur est **up ET** l'état rapporté par l'agent est frais ; `503` sinon (down, ou agent muet depuis > 10 min). |

Dans Uptime Kuma, créez un monitor **HTTP(s)** par serveur pointant sur `/api/public/health/<nom>`. Un `503` = serveur à relancer **ou** agent en panne — dans les deux cas, une alerte est justifiée.

## 2. Push (dead-man / indicateurs poussés par l'agent)

L'agent peut pousser des heartbeats vers des monitors **Push** Uptime Kuma :

- **Vie de l'agent** : un push régulier ; l'absence de heartbeat = agent mort.
- **Mise à jour disponible** (par jeu) : `up` = à jour, `down` + message = mise à jour dispo.

Format d'URL de push :

```
https://<votre-uptime-kuma>/api/push/<token>?status=up|down&msg=<message-encodé>
```

Renseignez ces URLs dans la config de l'agent :

- `kuma_agent_push` : heartbeat de vie de l'agent.
- `kuma_maj_push` (par serveur) : indicateur « mise à jour dispo ».

> Créez chaque monitor Push dans votre Uptime Kuma, récupérez son token, et collez l'URL correspondante. Les tokens sont propres à **votre** installation — ne les committez pas.

## Recommandations d'intervalle

- Monitors Push « mise à jour » : intervalle large (ex. 1800 s).
- Heartbeat agent : aligné sur le cycle de l'agent (2 min) avec une marge (ex. 600 s) avant de déclarer un down.
