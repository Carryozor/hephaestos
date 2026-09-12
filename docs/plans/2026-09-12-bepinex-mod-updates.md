# Plan d'implémentation : détection + mise à jour manuelle des mods BepInEx (Valheim)

## 1. Vue d'ensemble

Valheim n'a pas de Workshop : ses 5 mods ont été posés à la main dans `C:\steam\steamapps\common\Valheim dedicated server\` le 12/09. On veut le **même pattern UX que les mods Workshop** : détection backend → badge « maj disponible » → bouton manuel. Aucune bascule automatique (décision actée).

| Étape | Qui | Pourquoi |
|---|---|---|
| Interroger Thunderstore/GitHub (JSON léger) | **backend** | seul à avoir `app.state.http_client`, caches TTL et `state.json` ; même place que `app/steam_workshop.py` |
| Calculer `update_available` | **backend** | miroir de `mods.mod_update_available` (`app/mods.py:48`) |
| Télécharger le zip, remplacer, stop/start, vérifier | **agent** | seul accès au FS Windows ; a déjà le réseau sortant (steamcmd, api.steamcmd.net) |

Le canal `write_file` n'est **pas** réutilisé : borné à 512 Ko (`app/routes_agent.py:31`, `agent/hephaestos-lib.ps1:540`) et whitelist d'extensions texte (`app/routes_files.py:22`). Le binaire ne transite jamais par le backend — l'ordre ne porte qu'une **URL**.

## 2. Décisions actées

| # | Décision | Raison |
|---|---|---|
| D1 | Détection + badge + bouton manuel, **jamais** d'auto-update | décision utilisateur (version client identique obligatoire) |
| D2 | L'ordre porte l'URL ; l'agent télécharge | contrainte Docker/Linux ↔ Windows ; cohérent avec `Install-WorkshopMod` (`agent/hephaestos-lib.ps1:1448`) |
| D3 | `installed_version` = version **de la source**, jamais la version d'assembly des logs | piège P1 |
| D4 | Rafraîchissement réseau adossé au **poll agent** (`GET /api/agent/orders`, 2 min, TTL 1 h), pas à `GET /api/servers` (polled 10 s) | 5 mods = 5 appels sans API bulk ; pire cas 5×15 s sur la requête qui peuple tout le dashboard. Précédent : `auto_enqueue_mod_updates`/`auto_enqueue_game_updates` (`app/routes_agent.py:82`) |
| D5 | **Un seul ordre** met à jour N mods en **un seul cycle stop/start** | la DLL est verrouillée par le process : chaque MAJ impose un arrêt ; N ordres = N redémarrages |
| D6 | Cible normalisée `BepInEx\plugins\<PackageName>\` ; le pack BepInEx s'écrase en place à la racine | suppression/réinstallation triviales. Le chargement récursif des sous-dossiers de `plugins\` est **vérifié empiriquement** (XPortal en `plugins\XPortal\` → `Loading [XPortal 1.2.25]` le 12/09) |
| D7 | Pas de garde « 0 joueur » sur le bouton | pour Valheim `players` = « connexions vues depuis le démarrage » (`Get-ValheimLogInfo`, `agent/hephaestos-lib.ps1:964`) : le garde serait mensonger. L'UI avertit, l'admin décide |

## 3. Pièges identifiés (à traiter, pas à découvrir en prod)

**P1 — Version source ≠ version d'assembly → badge faux permanent.** `ValheimPlus_Grantapher_Temporary` est en **`10.1.1`** sur Thunderstore mais s'annonce `Valheim Plus 0.10.1.1` dans `LogOutput.log` ; idem pack `5.4.2350` (Thunderstore) vs `BepInEx 5.4.23.2`. Seeder `0.10.1.1` allumerait le badge à vie. → **Phase 0 bloquante** : relever les chaînes exactes des API et seeder avec elles. La version d'assembly va dans un champ séparé `plugin_version` (affichage/vérif log uniquement).

**P2 — Vérification post-restart qui ne peut pas échouer.** `LogOutput.log` contient déjà un `Chainloader startup complete` du boot précédent : le relire après un restart raté renverrait « OK ». → exiger `LastWriteTimeUtc >= instant du Start-GameServer` avant de faire confiance au contenu (règle /opt/CLAUDE.md n°9).

**P3 — Ne pas échouer sur les `[Error]` cosmétiques.** Un Valheim headless produit toujours des `[Error]` (shaders, cinématique d'intro), même sans mods (constaté le 12/09). Critères d'échec : absence de `Chainloader startup complete`, ou plugin attendu absent de `Loading [<Nom> <version>]`.

**P4 — Double chargement de plugin.** Les DLL actuelles sont **à plat** (`plugins\ValheimPlus.dll`, `Jotunn.dll`, `EquipmentAndQuickSlots.dll`) sauf XPortal (dossier). Extraire la nouvelle version dans `plugins\<Package>\` sans supprimer l'ancien fichier à plat ⇒ deux fois le même GUID. → chaque entrée porte `installed_paths` (seedés depuis l'install manuelle réelle) ; la suppression précède la copie et **échoue l'ordre si elle échoue**.

**P5 — Chemin fourni par l'agent, réutilisé comme cible de suppression.** L'agent rapporte les chemins écrits → stockés → renvoyés dans le prochain ordre comme `previous_paths` que l'agent **supprime**. Double validation obligatoire : rejet backend à l'ingestion (`..`, absolu, hors `BepInEx/plugins/`) ET re-validation agent avant `Remove-Item` (même idiome que `_reject_path_traversal` `app/routes_files.py:27` + `Invoke-ReadFile` `agent/hephaestos-lib.ps1:529`).

**P6 — URL de téléchargement = exécution de code sur la machine Windows.** Le slug seul vient du client ; l'URL vient **toujours** de la réponse API. Allowlist d'hôtes backend *et* agent, https obligatoire, taille max (100 Mo), garde zip-slip. Aucune des deux API ne fournit de checksum → pas de vérif d'intégrité possible, à assumer explicitement. GitHub redirige vers `*.githubusercontent.com` : l'allowlist doit couvrir la cible de redirection.

**P7 — Dérive de déploiement agent (incident 09/09).** La tâche planifiée a exécuté `ssc-agent.ps1` pendant 7 semaines sans que ça se voie, `agent_version` étant identique dans les deux fichiers. → bump obligatoire `$script:HephAgentVersion` (`agent/hephaestos-agent.ps1:17`) `2.2.0` → `2.3.0`, et vérification de déploiement = `agent_meta.agent_version == "2.3.0"` lu via l'API (falsifiable), pas un `scp` réussi.

**P8 — Télécharger AVANT d'arrêter le serveur.** Sinon un Thunderstore down laisse le serveur éteint pour rien.

## 4. Modèle de données

### 4.1 `state.json` — nouvelle racine `bepinex_mods`

```jsonc
"bepinex_mods": {
  "valheim": {
    "denikson/BepInExPack_Valheim": {
      "slug": "denikson/BepInExPack_Valheim",
      "title": "BepInExPack Valheim",
      "source": "thunderstore",          // thunderstore | github_release
      "owner": "denikson",
      "package": "BepInExPack_Valheim",
      "asset_pattern": null,             // github_release : ex "^XPortal-v.*\\.zip$"
      "tag_prefix": null,                // github_release : "v" (retiré pour normaliser)
      "target": "root",                  // root (pack) | plugins
      "installed_version": "5.4.2350",   // TOUJOURS la version de la SOURCE (P1/D3)
      "plugin_version": null,            // version d'assembly vue dans LogOutput.log (affichage)
      "installed_paths": ["BepInEx/core", "winhttp.dll", "doorstop_config.ini"],
      "installed_at": "2026-09-12T11:00:00+00:00",
      "latest_version": null,
      "download_url": null,
      "latest_checked_at": null,
      "last_error": null                 // n'efface jamais latest_version
    }
  }
}
```

`installed_paths` du pack (`target: "root"`) est **informatif** : rien n'est jamais supprimé à la racine (on écraserait `BepInEx/plugins` = tous les autres mods).

### 4.2 Seed versionné — `deploy/bepinex-mods.json`

Même convention que `deploy/servers.json` : `HEPHAESTOS_BEPINEX_FILE` (défaut `/data/bepinex-mods.json`) → `Settings.bepinex_mods` → `store.bepinex.seed_if_empty(...)` dans `create_app` (miroir de `app/main.py:35`).

| slug | source | target | version installée (à figer en phase 0) | `installed_paths` |
|---|---|---|---|---|
| `denikson/BepInExPack_Valheim` | thunderstore | root | `5.4.2350` | `BepInEx/core`, `winhttp.dll`, `doorstop_config.ini` |
| `Grantapher/ValheimPlus_Grantapher_Temporary` | thunderstore | plugins | **`10.1.1`** (⚠ pas `0.10.1.1`, cf. P1) | `BepInEx/plugins/ValheimPlus.dll` |
| `ValheimModding/Jotunn` | thunderstore | plugins | `2.30.0` | `BepInEx/plugins/Jotunn.dll`, `BepInEx/plugins/Jotunn.xml` |
| `RandyKnapp/EquipmentAndQuickSlots` | thunderstore | plugins | `3.1.2` | `BepInEx/plugins/EquipmentAndQuickSlots.dll` |
| `SpikeHimself/XPortal` | github_release (`tag_prefix:"v"`, `asset_pattern:"^XPortal-v.*\\.zip$"`) | plugins | `1.2.25` | `BepInEx/plugins/XPortal` |

### 4.3 Nouvel ordre — `update_bepinex_mods`

Ajouté à `ORDER_TYPES` (`app/storage.py:11`). Payload :

```jsonc
{"mods": [
  {"slug": "ValheimModding/Jotunn", "title": "Jötunn", "package": "Jotunn",
   "version": "2.31.0", "download_url": "https://thunderstore.io/package/download/...",
   "target": "plugins", "previous_paths": ["BepInEx/plugins/Jotunn.dll", "BepInEx/plugins/Jotunn.xml"],
   "previous_version": "2.30.0", "expected_plugin": "Jotunn"}
]}
```

`reject_if=lambda o: True` (un seul ordre BepInEx en vol par serveur), 409 sinon — idiome de `_create_order` (`app/routes_admin.py:80`). Compat : un agent < 2.3.0 tombe dans le `default` du switch (`agent/hephaestos-agent.ps1:524`) et rapporte `failed — type d'ordre inconnu`, ce qui déclenche déjà l'alerte Discord (`app/routes_agent.py:128`) — dégradation propre, à couvrir par un test.

## 5. Fichiers créés / modifiés

**Créés** : `app/storage_bepinex.py` (`BepInExRepository` — fichier séparé car `app/storage.py` fait déjà 810 lignes, limite projet 800), `app/bepinex_sources.py` (miroir de `app/steam_workshop.py`), `app/bepinex.py` (miroir de `app/mods.py`), `app/routes_bepinex.py` (miroir de `app/routes_files.py`), `deploy/bepinex-mods.json`, `tests/test_bepinex_{storage,sources,service,routes}.py`, `agent/tests/bepinex.Tests.ps1`.

**Modifiés** : `app/config.py` (+`bepinex_mods`), `deploy/entrypoint.py` (chargement non fatal si absent, contrairement à `servers.json`), `app/main.py` (seed + router), `app/storage.py` (`ORDER_TYPES`, `self.bepinex`), `app/routes_admin.py` (champs bepinex dans `list_servers`, lecture pure), `app/routes_agent.py` (`OrderResult.bepinex_installed`, refresh dans `get_orders`, persistance dans `report_order`), `app/static/app.js`, `agent/hephaestos-lib.ps1` (8 fonctions), `agent/hephaestos-agent.ps1` (dispatch + `extra` + version 2.3.0), `docs/agent-windows.md`, `docs/backend-setup.md`, `tests/js/render-smoke.js`, `tests/test_ui.py`.

## 6. Phases (chacune livrable seule)

### Phase 0 — Relevé des versions réelles (30 min, BLOQUANTE)
0.1 `curl -s https://thunderstore.io/api/experimental/package/<owner>/<name>/ | jq '{v:.latest.version_number, url:.latest.download_url}'` pour les 4 packages + `curl -s https://api.github.com/repos/SpikeHimself/XPortal/releases/latest | jq '{tag:.tag_name, assets:[.assets[].browser_download_url]}'`.
0.2 Noter la chaîne exacte de `version_number` **et** l'hôte réel du `download_url` (alimente l'allowlist P6).
0.3 Croiser avec ce qui est réellement sur disque (`ssh ssc-windows` : `.ps1` + `scp` + `-File`, **jamais** de `|` inline — piège cmd.exe confirmé 2×) : contenu de `BepInEx\plugins\` et lignes `Loading [<X> <v>]` de `BepInEx\LogOutput.log`.
0.4 Figer `deploy/bepinex-mods.json`. **Critère** : après la phase 3, aucun des 5 mods n'affiche `update_available` si aucune release n'est sortie entre-temps.

### Phase 1 — Socle de persistance (invisible)
**RED** `tests/test_bepinex_storage.py` : `seed_if_empty` crée les 5 entrées et est idempotent (2e appel sans réécriture, serveur déjà présent jamais re-seedé) ; `seed_if_empty({})` = no-op ; `all("palworld")` → `{}` ; `set_latest` préserve `installed_version`/`installed_paths` (miroir `set_mod_metadata`, `app/storage.py:555`) ; `set_latest(error=...)` pose `last_error` sans effacer un `latest_version` connu ; `set_installed` met à jour version+paths+`installed_at` et efface `last_error` ; `set_installed` sur slug inconnu = no-op (pas d'entrée fantôme, même règle que le commentaire `app/storage.py:196`) ; `installed_paths` avec `..`/absolu/hors `BepInEx/plugins/` → `ValueError` (P5).
**GREEN** `app/storage_bepinex.py` (délègue à `Store._lock/_load/_dump` comme `ModsRepository`), `app/config.py`, `deploy/entrypoint.py`, `app/main.py`, `ORDER_TYPES`. Risque : faible.

### Phase 2 — Sources réseau (invisible)
**RED** `tests/test_bepinex_sources.py` (httpx.MockTransport, comme `tests/test_steam_workshop.py:15`) : URL exacte appelée ; `{"version","download_url"}` extraits de `latest.*` ; 404 → `BepInExPackageNotFound` ; 500/timeout/JSON invalide → `BepInExFetchError` (jamais d'exception httpx nue) ; `download_url` hors allowlist → `BepInExUntrustedUrl` ; GitHub : `tag_name "v1.2.25"` + `tag_prefix "v"` → `1.2.25`, asset choisi par `asset_pattern`, zéro asset → `BepInExAssetNotFound` ; `version_is_newer("2.31.0","2.30.0")` vrai, `("2.30.0","2.30.0")` faux, **`("2.9.0","2.30.0")` faux** (comparaison numérique, pas lexicographique), versions non parsables → repli `!=` documenté et testé.
**GREEN** `app/bepinex_sources.py` + `version_is_newer` dans `app/bepinex.py`. Risque : moyen — fixtures **copiées des vraies réponses** de la phase 0, pas inventées.

### Phase 3 — Service + exposition API (première valeur observable)
**RED** `tests/test_bepinex_service.py` + `tests/test_bepinex_routes.py` : `GET /api/servers` expose `bepinex_mods[]` + `bepinex_update_count` pour `valheim` et **aucune** clé `bepinex_*` pour `palworld` (non-régression) ; `GET /api/servers` ne fait **aucun** appel réseau bepinex (transport mock qui `assert False`) — D4 ; `refresh_bepinex_versions` = un appel par mod, persistance, bascule d'`update_available` ; TTL = un seul aller-retour par mod sur deux appels, **TTL posé même en cas d'échec réseau** (copie de `refresh_mods_steam_dates`, `app/mods.py:76`) ; échec réseau → `latest_version` conservé + `last_error` ; `GET /api/agent/orders` déclenche le refresh et **une exception du refresh ne fait jamais échouer le poll** (même contrat que `app/mods.py:250`) ; serveur sans mods = no-op.
**GREEN** `app/bepinex.py`, hook dans `app/routes_agent.py:82`, bloc dans `app/routes_admin.py:60`. Risque : moyen — jamais d'exception qui remonte dans `/api/servers`.

### Phase 4 — UI lecture seule (badge, sans bouton)
Risque nul côté prod : aucun chemin d'écriture n'existe encore.
**RED** `tests/js/render-smoke.js` + `tests/test_ui.py` : `renderBepInExSummary(s)` = `""` sans `bepinex_mods`, liste + compteur sinon ; `renderCard` affiche le bandeau pour un serveur **sans** `workshop_appid` mais **avec** `bepinex_mods` (le garde actuel `if ("workshop_appid" in s)` `app/static/app.js:289` exclut Valheim — c'est le point exact à corriger) ; `renderDetailMods` ne passe plus `detailColumns` en `single` quand `bepinex_mods` est non vide (`app/static/app.js:1117`) ; un mod à jour dispo affiche `installed → latest` ; l'annonciateur `annModsValue` (`app/static/app.js:232`) somme aussi les mods BepInEx ; `test_app_js_served_with_ui_hooks` += `renderBepInExSummary`, `updateBepInExMods`.
**GREEN** `bepinexUpdateState(m)` (miroir `modUpdateState`:102), `renderBepInExSummary`, extensions de `renderCard`/`renderDetailMods`/annonciateur, et `.bepinex-detail` ajouté aux sélecteurs ignorés du clic carte (`app/static/app.js:340`).

### Phase 5a — Agent : bibliothèque BepInEx (Pester, sans dispatch)
**RED** `agent/tests/bepinex.Tests.ps1` (stubs sur `TestDrive`, style `files.Tests.ps1`/`workshop-mods.Tests.ps1`) :
- *URL* : `Test-BepInExUrlAllowed` accepte l'allowlist en https, rejette http, hôte inconnu, URL avec credentials `@`, IP littérale. `Invoke-BepInExDownload` (wrapper mockable, motif `Invoke-Schtasks`:1555) rejette avant toute requête, échoue au-delà de la borne de taille, et est validé **par la présence du fichier**, pas par le code retour.
- *Extraction/placement* : zip contenant `../evil.dll` → throw, rien d'écrit ; `Copy-BepInExPayload -Target plugins` sur zip plat → `BepInEx\plugins\<Package>\X.dll`, ignore `manifest.json`/`icon.png`/`README.md`, renvoie les chemins relatifs (`/`) ; zip avec dossier `plugins/` → contenu dans `BepInEx\plugins\<Package>\` ; `-Target root` sur zip à wrapper unique (`BepInExPack_Valheim/`) → wrapper retiré, `BepInEx/core`+`winhttp.dll`+`doorstop_config.ini` écrasés, **`BepInEx\plugins` existant intact** (test qui pose un faux plugin avant et vérifie sa survie — le scénario catastrophe) ; `Remove-BepInExPaths` throw sur `..`/absolu/hors `plugins`, no-op sur chemin absent.
- *Log* : fichier absent → `ChainloaderComplete=$false` sans exception ; **`LastWriteTimeUtc` antérieur au `MinWriteTimeUtc` → `$false` même si la ligne est là (P2, test central)** ; log valide → `Plugins` = `{Name,Version}` parsés ; `[Error]` cosmétiques + chainloader complet → succès (P3).
- *Orchestration* `Update-BepInExMods` : URL invalide → `ok=$false` et **`Stop-GameServer` jamais appelé** (P8) ; échec de téléchargement d'un mod sur deux → rien d'installé, serveur pas arrêté ; nominal → ordre `Backup → Remove → Copy → Start`, `bepinex_installed` complet ; chainloader absent → restauration + `Start-GameServer` rejoué + `ok=$false` (miroir du rollback `Update-GameServer`, `agent/hephaestos-lib.ps1:1897`) ; `Start-GameServer` qui throw pendant le rollback est capturé (piège F2, `agent/hephaestos-lib.ps1:1852`) ; plugin attendu absent → `ok=$false` + rollback.

**GREEN** — nouvelles fonctions `agent/hephaestos-lib.ps1` : `Test-BepInExUrlAllowed`, `Invoke-BepInExDownload`, `Expand-BepInExPackage`, `Copy-BepInExPayload`, `Remove-BepInExPaths`, `Backup-BepInExState`, `Restore-BepInExBackup`, `Get-BepInExLogStatus`, `Update-BepInExMods`. Séquence **non négociable** :
1. valider les URL, télécharger+extraire **tous** les mods en staging (`$env:TEMP\hephaestos-bepinex\<guid>`) ;
2. `Backup-GameSave -Kind "pre-bepinex"` (best-effort, noté dans le detail) ;
3. `Stop-GameServer` ;
4. `Backup-BepInExState` (zip de `BepInEx\core`, `plugins`, `config`, `winhttp.dll`, `doorstop_config.ini`, rotation comme `Get-GameSaveBackups`) — **bloquant** : pas de backup, pas de MAJ ;
5. `Remove-BepInExPaths` (plugins seulement) puis `Copy-BepInExPayload` par mod ;
6. `$startedAt = (Get-Date).ToUniversalTime()` puis `Start-GameServer` ;
7. poll `Get-BepInExLogStatus -MinWriteTimeUtc $startedAt` toutes les 5 s, 180 s max ;
8. échec → `Restore-BepInExBackup` + `Start-GameServer` (try/catch) + `ok=$false` ;
9. succès → `ok=$true`, `bepinex_installed`, `detail` listant `slug: avant → après` + plugins réellement chargés.
Nettoyage du staging dans un `finally`. Risque : **élevé** — rollback testé avant tout essai réel.

### Phase 5b — Ordre bout en bout + bouton
**RED** : `agent/tests/agent.Tests.ps1` (nouveau `Describe`, calqué sur « ordres mods » ligne 737) — ordre `update_bepinex_mods` → `Update-BepInExMods` appelé une fois avec les N mods, statut `done`, `Body.bepinex_installed` transmis ; `ok=$false` → `failed` + detail ; exception → `failed` avec le message.
`tests/test_bepinex_routes.py` — `POST /api/servers/valheim/bepinex/update` (body `{"slugs":[...]}` ou `{"all":true}`) → 201, **un** ordre avec `mods[]` complet ; 2e appel → 409 ; slug inconnu → 400 ; serveur sans mods BepInEx → 404 ; serveur inconnu → 404 ; mod sans `latest_version` → 409 explicite plutôt qu'un ordre vide ; **l'URL de l'ordre vient du store, pas du body** (test qui envoie un `download_url` malveillant et vérifie qu'il est ignoré — P6) ; rôle `user` → 403. `POST .../bepinex/check` force le refresh hors TTL (admin). `POST /api/agent/orders/<id>` avec `bepinex_installed` → version/paths/`installed_at` mis à jour, `update_available` retombe à faux ; chemin `..` → rejeté, rien persisté ; ordre `failed` → rien modifié + alerte Discord (existant).
**GREEN** : `app/routes_bepinex.py`, `app/bepinex.enqueue_bepinex_update_order`, `OrderResult.bepinex_installed` + persistance dans `report_order` (même emplacement que `list_files`/`read_file`, `app/routes_agent.py:116`), dispatch agent (`agent/hephaestos-agent.ps1:470` et `:534`), bump `2.3.0`, et `updateBepInExMods(name, slugs)` avec un `confirmDialog` disant explicitement : **« le serveur va être arrêté puis redémarré ; les joueurs devront installer la même version côté client, sinon "Incompatible Version" »**.

### Phase 6 (optionnelle) — Versions réellement chargées
`Send-HephStateReport` (`agent/hephaestos-agent.ps1:268`) ajoute `bepinex_plugins: [{name,version}]` quand `BepInEx\LogOutput.log` existe (réutilise `Get-BepInExLogStatus`). `ServerState` gagne le champ avec bornes strictes (≤ 50 entrées, chaînes contraintes — l'agent est une source non fiable pour l'UI, cf. `app/routes_agent.py:24`). UI : « chargé : X 1.2.3 » + badge « désynchronisé ». **Ne gate jamais `update_available`** (P1).

## 7. Stratégie de test

| Niveau | Emplacement | Couverture |
|---|---|---|
| Unitaire Python | `tests/test_bepinex_{storage,sources,service}.py` | parsing API (mock httpx), TTL, comparaison de versions, validation de chemins |
| Intégration API | `tests/test_bepinex_routes.py` + ajouts `test_admin.py`/`test_agent_api.py` | ordres, 409/404/403, persistance du rapport agent, non-régression Palworld |
| Rendu JS | `tests/js/render-smoke.js` + `tests/test_ui.py` | badge, bandeau, colonne détail, annonciateur |
| Pester lib | `agent/tests/bepinex.Tests.ps1` | allowlist, zip-slip, placement, suppression, log, rollback |
| Pester dispatch | `agent/tests/agent.Tests.ps1` | ordre → fonction → statut → extra |

Commandes : `pytest` (racine, `pytest.ini` gère `pythonpath`), `node tests/js/render-smoke.js`, `Invoke-Pester agent/tests` (206 verts au 09/09 — barre : aucun test cassé). Couverture ≥ 80 % sur les nouveaux modules.

## 8. Vérification réelle (aucune optionnelle)

1. **Agent déployé** : `agent_meta.agent_version == "2.3.0"` lu via l'API (P7) — un `scp` réussi ne prouve rien.
2. **Détection** : après un cycle, les 5 mods ont un `latest_checked_at` récent et `update_available` **faux** partout (phase 0 réussie).
3. **MAJ réelle** : sur **un seul mod à faible risque d'abord** (Jötunn ou EAQS, jamais le pack BepInEx en premier essai), serveur vide ; vérifier *après* : `Chainloader startup complete` + `Loading [<mod> <nouvelle version>]` dans `LogOutput.log`, absence de l'ancien fichier à plat dans `plugins\` (P4), process up, badge retombé.
4. **Rollback** : au moins une répétition provoquée prouvant que le serveur redémarre sur l'ancienne version.
5. Wiki Outline + note de mémoire après coup.

## 9. Risques & mitigations

| Risque | Gravité | Mitigation |
|---|---|---|
| Badge faux permanent (P1) | élevée | phase 0 bloquante + test figé sur les vraies chaînes |
| Serveur cassé après MAJ | élevée | backup bloquant + vérif log horodatée + rollback auto + 1er essai sur mod secondaire |
| Double chargement de plugin (P4) | moyenne | `installed_paths` seedés, suppression bloquante avant copie, vérif de la liste chargée |
| URL/zip malveillant (P6) | élevée | slug seul côté client, URL issue de l'API, allowlist double, https, borne de taille, garde zip-slip |
| Thunderstore/GitHub down | faible | `last_error` affiché, `latest_version` conservé, TTL posé quand même, téléchargement avant arrêt |
| Latence dashboard | moyenne | D4 : zéro réseau dans `GET /api/servers` |
| Dérive agent (P7) | moyenne | bump de version + vérification par l'API |

## 10. Critères de succès

- [ ] Phase 0 : les 5 versions du seed correspondent exactement aux API et au disque ; zéro badge au démarrage.
- [ ] `GET /api/servers` expose `bepinex_mods[].update_available` pour Valheim, sans réseau, sans modifier la sortie Palworld/Windrose.
- [ ] Une release réelle allume le badge dans les 2 min suivant le poll agent.
- [ ] Le bouton crée **un** ordre ; l'agent télécharge, sauvegarde, remplace, redémarre, confirme via `LogOutput.log` horodaté.
- [ ] Un échec restaure l'état précédent, laisse le serveur **up**, marque `failed`, alerte Discord.
- [ ] Aucune MAJ auto n'est jamais créée pour un mod BepInEx (test explicite : `auto_enqueue_mod_updates` ignore les serveurs sans `workshop_appid`).
- [ ] pytest + Pester + render-smoke verts, ≥ 80 % sur les nouveaux modules.
- [ ] `docs/agent-windows.md`, `docs/backend-setup.md`, wiki, mémoire à jour.

## 11. Points à trancher avant de coder

1. **Périmètre d'accès** : `require_admin_role` (comme `app/routes_files.py`) ou `user` assignés (comme les mods Workshop) ? Recommandation : `require_admin_role` — l'action arrête le serveur et installe du code.
2. **Ajout/retrait de mods BepInEx depuis l'UI** : hors périmètre ici (registre seedé/édité à la main). À confirmer comme « plus tard ».
3. **Rotation des backups BepInEx** : dossier de backups de saves mutualisé ou sous-dossier dédié ? (impacte `save_backups` remonté à l'UI — ne pas polluer la liste des saves restaurables).

---

### Découvertes qui changent le design par rapport au brief

- **P1 est le point le plus important** : `installed_version` doit être la version *Thunderstore* (`10.1.1` pour Valheim Plus), pas la version installée annoncée dans le brief (`0.10.1.1`, qui est la version d'assembly). Sans la phase 0, la feature livre 5 badges faux.
- **Le rafraîchissement ne doit pas vivre dans `GET /api/servers`** (polled toutes les 10 s, pas d'API bulk chez Thunderstore) mais dans `GET /api/agent/orders`, où les deux auto-enqueue existants sont déjà accrochés.
- **Un seul ordre pour N mods** : contrairement aux mods Workshop (install_mod × N + 1 restart), un plugin BepInEx ne peut pas être remplacé à chaud (DLL verrouillée) — donc l'ordre porte lui-même le stop/start, et grouper évite N redémarrages.
- **Chaîne de confiance sur les chemins** (P5) et **allowlist d'URL** (P6) : l'agent supprime des chemins et exécute du code téléchargé, les deux doivent être validés côté backend *et* côté agent.
