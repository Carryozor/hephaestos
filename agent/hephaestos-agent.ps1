# hephaestos-agent.ps1 -- boucle principale de l'agent Hephaestos.
# Un passage = un cycle complet (etat -> kuma -> ordres -> auto-update) puis le script
# se termine ; c'est la tache planifiee (schtasks, toutes les 2 min) qui cadence les cycles.
# PS 5.1-compatible : memes pieges que hephaestos-lib.ps1 ("${Var}?..." avant un "?" litteral,
# variables non definies qui s'interpolent en vide sans erreur).

param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot "hephaestos-config.json"),
    [datetime]$Now = (Get-Date),
    [string]$LogPath = "C:\hephaestos\hephaestos-agent.log"
)

. (Join-Path $PSScriptRoot "hephaestos-lib.ps1")

# Version rapportee dans le POST /api/agent/state (agent_version) -- a incrementer a
# chaque changement de contrat/comportement observable par le backend.
$script:HephAgentVersion = "2.2.0"

function Write-HephLog {
    <#
    .SYNOPSIS
        Ajoute une ligne horodatee au fichier de log, avec rotation (tronque a 1 Mo).
    .NOTES
        Rotation simple : si le fichier existant depasse 1 Mo AVANT l'ecriture, il est
        vide avant d'ecrire la nouvelle ligne (pas d'archive, juste eviter la croissance
        indefinie sur C:\hephaestos).

        Horodatage : utilise TOUJOURS (Get-Date) au moment de l'ecriture, jamais le $Now
        fige du cycle -- un cycle long (steamcmd) doit produire des timestamps differents
        entre "cycle demarre" et "done", sinon la reconstruction de timeline en cas
        d'incident est impossible. $Now (parametre du cycle) reste reserve a la logique de
        fenetre auto_update_window.

        Masquage : tout token Kuma present dans le message ("/api/push/<token>", ex. via un
        message d'exception Send-KumaPush qui embarque l'URL complete) est remplace par
        "/api/push/***" avant l'ecriture -- le fichier de log n'est pas un secret store.
    #>
    param(
        [Parameter(Mandatory)]
        [string]$LogPath,

        [Parameter(Mandatory)]
        [string]$Message
    )

    $dir = Split-Path -Path $LogPath -Parent
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }

    if (Test-Path -LiteralPath $LogPath) {
        $existing = Get-Item -LiteralPath $LogPath
        if ($existing.Length -gt 1MB) {
            Set-Content -LiteralPath $LogPath -Value "" -NoNewline
        }
    }

    $maskedMessage = $Message -replace "(/api/push/)[^?&\s]+", '$1***'
    $line = "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))] $maskedMessage"
    Add-Content -LiteralPath $LogPath -Value $line
}

function Test-InAutoUpdateWindow {
    <#
    .SYNOPSIS
        Vrai si $Now (heure locale) tombe dans la fenetre auto_update_window (bornes incluses).
    #>
    param(
        $Window,

        [Parameter(Mandatory)]
        [datetime]$Now
    )

    if ($null -eq $Window) {
        return $false
    }

    $startTs = [timespan]::Parse($Window.start)
    $endTs = [timespan]::Parse($Window.end)
    $nowTs = $Now.TimeOfDay

    return ($nowTs -ge $startTs -and $nowTs -le $endTs)
}

function Set-HephOrderStatus {
    <#
    .SYNOPSIS
        POST /api/agent/orders/{id} avec status+detail. Renvoie $false si l'ordre est deja
        dans un etat terminal (409 -- deja gere ailleurs, l'appelant ne doit pas executer
        l'action), $true dans tous les autres cas (succes ou erreur transitoire loggee).
    #>
    param(
        [Parameter(Mandatory)]
        $Cfg,

        [Parameter(Mandatory)]
        [string]$OrderId,

        [Parameter(Mandatory)]
        [string]$Status,

        $Detail = $null,

        $Extra = $null,

        [string]$LogPath
    )

    $body = @{ status = $Status; detail = $Detail }
    if ($Extra) {
        foreach ($key in $Extra.Keys) { $body[$key] = $Extra[$key] }
    }

    try {
        Invoke-HephApi -Cfg $Cfg -Method Post -Path "/api/agent/orders/${OrderId}" -Body $body | Out-Null
        return $true
    } catch {
        if ($_.Exception.Message -match "409") {
            Write-HephLog -LogPath $LogPath -Message "ordre ${OrderId}: deja en etat terminal (409), ignore"
            return $false
        }
        Write-HephLog -LogPath $LogPath -Message "ordre ${OrderId}: ERREUR report statut ${Status}: $($_.Exception.Message)"
        return $true
    }
}

function Send-HephStateReport {
    <#
    .SYNOPSIS
        Collecte l'etat de tous les serveurs (buildid, process, joueurs, mods, metriques)
        et le POST sur /api/agent/state. Renvoie les buildids locaux et comptages joueurs
        pour reutilisation (pushes kuma, auto-update).
    .NOTES
        Appelee en debut de cycle ET apres chaque ordre execute : l'UI reflete le
        resultat reel d'un restart/update en quelques secondes au lieu d'attendre le
        tick planifie suivant (jusqu'a 2 min de retour perime).
    #>
    param(
        [Parameter(Mandatory)]
        $Cfg,

        [string]$LogPath = "C:\hephaestos\hephaestos-agent.log"
    )

    $localBuildIds = @{}
    $playerCounts = @{}
    $stateReport = @{}

    foreach ($serverCfg in $Cfg.servers) {
        try {
            $buildid = $null
            try {
                $manifestPath = Get-ManifestPath -SteamRoot $Cfg.steamcmd_root -AppId $serverCfg.appid
                $buildid = Get-LocalBuildId -ManifestPath $manifestPath
            } catch {
                Write-HephLog -LogPath $LogPath -Message "[$($serverCfg.name)] buildid local indisponible: $($_.Exception.Message)"
            }
            $localBuildIds[$serverCfg.name] = $buildid

            $proc = Get-Process -Name $serverCfg.process -ErrorAction SilentlyContinue
            $processUp = [bool]$proc

            $rconInfo = $null
            $processCpuPercent = $null
            $processMemMb = $null
            if ($processUp) {
                try {
                    $rconInfo = Get-ServerRconInfo -Cfg $Cfg -ServerCfg $serverCfg
                } catch {
                    Write-HephLog -LogPath $LogPath -Message "[$($serverCfg.name)] RCON Info echoue: $($_.Exception.Message)"
                }
                try {
                    $metrics = Get-ProcessMetrics -ProcessName $serverCfg.process
                    $processCpuPercent = $metrics.CpuPercent
                    $processMemMb = $metrics.MemMb
                } catch {
                    Write-HephLog -LogPath $LogPath -Message "[$($serverCfg.name)] metriques process echouees: $($_.Exception.Message)"
                }
            }

            $players = $null
            $playersList = $null
            if ($serverCfg.PSObject.Properties.Name -contains "rcon" -and $serverCfg.rcon) {
                try {
                    $result = Get-PalworldPlayers -Cfg $Cfg -ServerCfg $serverCfg
                    $players = $result.Count
                    if ($null -ne $result.Count) {
                        $playersList = @($result.Players | ForEach-Object {
                            @{ id = $_.playeruid; name = $_.name; steamid = $_.steamid }
                        })
                    }
                } catch {
                    Write-HephLog -LogPath $LogPath -Message "[$($serverCfg.name)] comptage joueurs echoue: $($_.Exception.Message)"
                }
            } elseif ($serverCfg.PSObject.Properties.Name -contains "windrose_plus" -and $serverCfg.windrose_plus) {
                try {
                    $result = Get-WindrosePlayers -Cfg $Cfg -ServerCfg $serverCfg
                    $players = $result.Count
                    if ($null -ne $result.Count) {
                        $playersList = @($result.Players | ForEach-Object {
                            @{ id = $_.session_id; name = $_.name; steamid = $_.steamid }
                        })
                    }
                } catch {
                    Write-HephLog -LogPath $LogPath -Message "[$($serverCfg.name)] comptage joueurs echoue: $($_.Exception.Message)"
                }
            } elseif ($serverCfg.PSObject.Properties.Name -contains "query_port" -and $serverCfg.query_port) {
                # A2S_INFO (jeux Source query, ex. Valheim : port de jeu + 1) : compte
                # seul, pas de liste nominative. Ne compte que si le process est up
                # (requete UDP inutile sinon).
                if ($processUp) {
                    try {
                        $players = Get-A2sPlayerCount -HostName "127.0.0.1" -Port ([int]$serverCfg.query_port)
                    } catch {
                        Write-HephLog -LogPath $LogPath -Message "[$($serverCfg.name)] comptage A2S echoue: $($_.Exception.Message)"
                    }
                }
            }
            $playerCounts[$serverCfg.name] = $players

            $saveBackups = $null
            try {
                if (Get-GameSaveDir -Cfg $Cfg -ServerCfg $serverCfg) {
                    $saveBackups = @(Get-GameSaveBackups -Cfg $Cfg -ServerCfg $serverCfg)
                }
            } catch {
                Write-HephLog -LogPath $LogPath -Message "[$($serverCfg.name)] liste des backups echouee: $($_.Exception.Message)"
            }

            $installedModIds = $null
            $processStartedAt = $null
            if ($serverCfg.PSObject.Properties.Name -contains "workshop_appid" -and $serverCfg.workshop_appid) {
                try {
                    $installedModIds = @(Get-InstalledWorkshopMods -Cfg $Cfg -ServerCfg $serverCfg)
                } catch {
                    Write-HephLog -LogPath $LogPath -Message "[$($serverCfg.name)] liste des mods echouee: $($_.Exception.Message)"
                }
            }
            if ($proc) {
                # ToUniversalTime() obligatoire : StartTime est en heure LOCALE Windows
                # (la machine est en UTC+2 l'ete) alors que le backend compare mods_changed_at
                # en UTC (isoformat Python) -- une comparaison de chaines ISO8601 avec des
                # offsets differents n'est pas fiable lexicographiquement, d'ou ce piege.
                $processStartedAt = $proc.StartTime.ToUniversalTime().ToString("o")
            }

            $stateReport[$serverCfg.name] = @{
                buildid             = $buildid
                process_up          = $processUp
                players             = $players
                players_list        = $playersList
                installed_mod_ids   = $installedModIds
                save_backups        = $saveBackups
                process_started_at  = $processStartedAt
                rcon_info           = $rconInfo
                process_cpu_percent = $processCpuPercent
                process_mem_mb      = $processMemMb
            }
        } catch {
            Write-HephLog -LogPath $LogPath -Message "[$($serverCfg.name)] ERREUR etat: $($_.Exception.Message)"
        }
    }

    try {
        $body = @{
            servers          = $stateReport
            agent_version    = $script:HephAgentVersion
            config_servers   = @($Cfg.servers)
            discovered_games = @(Get-DiscoveredGames -Cfg $Cfg)
        }
        if ($Cfg.PSObject.Properties.Name -contains "backend_config_hash") {
            $body.config_hash = $Cfg.backend_config_hash
        }
        Invoke-HephApi -Cfg $Cfg -Method Post -Path "/api/agent/state" -Body $body | Out-Null
    } catch {
        Write-HephLog -LogPath $LogPath -Message "ERREUR POST /api/agent/state: $($_.Exception.Message)"
    }

    return @{ LocalBuildIds = $localBuildIds; PlayerCounts = $playerCounts }
}

function Invoke-HephAgentCycle {
    <#
    .SYNOPSIS
        Un cycle complet de l'agent : etat, kuma, drainage des ordres, auto-update.
    .NOTES
        Toute erreur sur un serveur est capturee localement (try/catch) et n'empeche pas
        le traitement des autres serveurs ni la suite du cycle -- verification qui peut
        echouer plutot qu'un crash silencieux du script planifie.

        -LockPath (optionnel) : chemin du verrou pose par Invoke-HephAgentCycleLocked,
        rafraichi avant chaque ordre pendant le drainage. Sans ce touch, une file de
        plusieurs updates depasserait les 10 min d'age max du verrou et le tick planifie
        suivant le volerait -- relançant steamcmd EN PARALLELE sur le meme serveur,
        exactement l'incident que le verrou existe pour eviter.
    #>
    param(
        [Parameter(Mandatory)]
        $Cfg,

        [datetime]$Now = (Get-Date),

        [string]$LogPath = "C:\hephaestos\hephaestos-agent.log",

        [string]$LockPath = $null,

        # Chemin du fichier de config sur disque -- necessaire pour persister la config
        # serveurs poussee par le backend (Update-HephServersFromBackend, plus bas dans ce
        # cycle). Defaut aligne sur celui du script pour un appel direct hors tests.
        [string]$ConfigPath = (Join-Path $PSScriptRoot "hephaestos-config.json")
    )

    Write-HephLog -LogPath $LogPath -Message "=== cycle demarre ==="

    $state = Send-HephStateReport -Cfg $Cfg -LogPath $LogPath
    $localBuildIds = $state.LocalBuildIds

    if ($Cfg.kuma_agent_push) {
        try {
            Send-KumaPush -PushUrl $Cfg.kuma_agent_push -Status "up" | Out-Null
        } catch {
            Write-HephLog -LogPath $LogPath -Message "ERREUR push kuma_agent_push: $($_.Exception.Message)"
        }
    }

    foreach ($serverCfg in $Cfg.servers) {
        try {
            $public = $null
            try {
                $public = Get-PublicBuildId -AppId $serverCfg.appid
            } catch {
                Write-HephLog -LogPath $LogPath -Message "[$($serverCfg.name)] buildid public indisponible: $($_.Exception.Message)"
            }

            if ($serverCfg.PSObject.Properties.Name -contains "kuma_maj_push" -and $serverCfg.kuma_maj_push) {
                $local = $localBuildIds[$serverCfg.name]
                if ($local -and $public) {
                    if ($local -eq $public) {
                        Send-KumaPush -PushUrl $serverCfg.kuma_maj_push -Status "up" -Msg "A_jour (build ${local})" | Out-Null
                    } else {
                        Send-KumaPush -PushUrl $serverCfg.kuma_maj_push -Status "down" -Msg "MAJ_dispo build ${local} -> ${public}" | Out-Null
                    }
                }
            }
        } catch {
            Write-HephLog -LogPath $LogPath -Message "[$($serverCfg.name)] ERREUR push kuma_maj_push: $($_.Exception.Message)"
        }
    }

    # Drainage sequentiel de la file d'ordres : ordre termine -> re-GET immediat ->
    # ordre suivant, sans attendre le tick planifie de 2 min entre chaque ordre.
    # Le re-GET a chaque iteration honore les annulations survenues entre-temps.
    # Un id n'est tente qu'UNE fois par cycle ($attemptedOrderIds) : un echec de report
    # de statut laisse l'ordre pending, et sans ce garde-fou la boucle le retenterait
    # immediatement a l'infini -- la cadence 2 min throttlait naturellement l'ancien
    # code mono-ordre. Plafond dur en ceinture-bretelles.
    $attemptedOrderIds = @{}
    $maxDrainIterations = 20
    for ($drainIteration = 0; $drainIteration -lt $maxDrainIterations; $drainIteration++) {
        $orders = @()
        try {
            $ordersResp = Invoke-HephApi -Cfg $Cfg -Method Get -Path "/api/agent/orders"
            $orders = @($ordersResp.orders)
        } catch {
            Write-HephLog -LogPath $LogPath -Message "ERREUR GET /api/agent/orders: $($_.Exception.Message)"
            break
        }

        # Le POST /api/agent/state (Send-HephStateReport, debut de cycle et apres chaque
        # ordre) precede TOUJOURS ce GET /api/agent/orders : le backend est ainsi enrichi
        # du snapshot avant de servir sa config -- invariant d'ordre du cycle, a ne pas
        # inverser. Applique separement du GET (try/catch propre, non bloquant pour le
        # drainage) : un echec d'ecriture de config ne doit pas faire perdre les ordres
        # deja recus dans $ordersResp. $ordersResp.config peut etre absent du mock
        # historique (orders = @($null) sans cle "config") : sous Set-StrictMode, un acces
        # direct a une propriete absente d'un pscustomobject leve une exception, d'ou la
        # garde PSObject.Properties.Name ci-dessous plutot qu'un acces nu.
        try {
            $backendConfig = $null
            if ($ordersResp.PSObject.Properties.Name -contains "config") {
                $backendConfig = $ordersResp.config
            }
            $Cfg = Update-HephServersFromBackend -Cfg $Cfg -ConfigPath $ConfigPath -BackendConfig $backendConfig -LogPath $LogPath
        } catch {
            Write-HephLog -LogPath $LogPath -Message "ERREUR application config backend: $($_.Exception.Message)"
        }

        $order = $orders |
            Where-Object { $_ -and $_.id -and -not $attemptedOrderIds.ContainsKey([string]$_.id) } |
            Select-Object -First 1
        if (-not $order) {
            break
        }
        $attemptedOrderIds[[string]$order.id] = $true

        if ($LockPath) {
            # Rafraichit le verrou avant chaque ordre : une file de plusieurs updates
            # depasserait sinon l'age max du verrou (10 min) et le tick suivant le
            # volerait en plein steamcmd.
            Set-Content -LiteralPath $LockPath -Value (Get-Date).ToString("o") -Force
        }

        # Ordres de deploiement : auto-porteurs (tout le contexte dans le payload),
        # traites AVANT le lookup Cfg.servers -- un serveur en installing/awaiting_setup
        # n'est pas encore dans la config poussee (seuls les "active" le sont).
        $deployTypes = @("install_game", "scan_exe", "setup_server")
        if ($order.type -in $deployTypes) {
            try {
                $proceed = Set-HephOrderStatus -Cfg $Cfg -OrderId $order.id -Status "running" -LogPath $LogPath
                if ($proceed) {
                    $result = $null
                    try {
                        switch ($order.type) {
                            "install_game" { $result = Invoke-InstallGame -Cfg $Cfg -AppId ([int]$order.appid) }
                            "scan_exe"     { $result = Invoke-ScanExe -Cfg $Cfg -AppId ([int]$order.appid) }
                            "setup_server" { $result = Invoke-SetupServer -Cfg $Cfg -Order $order }
                        }
                    } catch {
                        $result = [pscustomobject]@{ ok = $false; detail = "exception: $($_.Exception.Message)" }
                    }
                    $finalStatus = if ($result.ok) { "done" } else { "failed" }
                    $extra = $null
                    if ($result.PSObject.Properties.Name -contains "exe_candidates" -and $result.exe_candidates) {
                        $extra = @{ exe_candidates = @($result.exe_candidates) }
                    }
                    Set-HephOrderStatus -Cfg $Cfg -OrderId $order.id -Status $finalStatus -Detail $result.detail -Extra $extra -LogPath $LogPath | Out-Null
                    Write-HephLog -LogPath $LogPath -Message "ordre $($order.id) [$($order.type)/$($order.server)]: ${finalStatus} -- $($result.detail)"
                }
            } catch {
                Write-HephLog -LogPath $LogPath -Message "ordre $($order.id): ERREUR traitement: $($_.Exception.Message)"
            }
            Send-HephStateReport -Cfg $Cfg -LogPath $LogPath | Out-Null
            continue
        }

        $serverCfg = $Cfg.servers | Where-Object { $_.name -eq $order.server } | Select-Object -First 1

        if (-not $serverCfg) {
            Write-HephLog -LogPath $LogPath -Message "ordre $($order.id): serveur '$($order.server)' inconnu de la config, ignore"
            continue
        }

        try {
            $proceed = Set-HephOrderStatus -Cfg $Cfg -OrderId $order.id -Status "running" -LogPath $LogPath

            if ($proceed) {
                $result = $null
                try {
                    switch ($order.type) {
                        "update" { $result = Update-GameServer -Cfg $Cfg -ServerCfg $serverCfg }
                        "restart" {
                            $note = Restart-GameServer -Cfg $Cfg -ServerCfg $serverCfg
                            $result = [pscustomobject]@{ ok = $true; detail = "redemarrage effectue${note}" }
                        }
                        "start" {
                            Start-GameServer -ServerCfg $serverCfg
                            $result = [pscustomobject]@{ ok = $true; detail = "demarrage effectue" }
                        }
                        "stop" {
                            Stop-GameServer -Cfg $Cfg -ServerCfg $serverCfg -Reason "Arret"
                            $result = [pscustomobject]@{ ok = $true; detail = "arret effectue" }
                        }
                        "install_mod" {
                            Install-WorkshopMod -Cfg $Cfg -ServerCfg $serverCfg -WorkshopId $order.workshop_id
                            $result = [pscustomobject]@{ ok = $true; detail = "mod $($order.workshop_id) installe" }
                        }
                        "remove_mod" {
                            Remove-WorkshopMod -Cfg $Cfg -ServerCfg $serverCfg -WorkshopId $order.workshop_id
                            $result = [pscustomobject]@{ ok = $true; detail = "mod $($order.workshop_id) retire" }
                        }
                        "backup" {
                            # Flush RCON best-effort avant le zip d'un serveur qui tourne
                            # (Palworld ecrit sa save periodiquement, on fige l'etat courant).
                            if ($serverCfg.PSObject.Properties.Name -contains "rcon" -and $serverCfg.rcon -and
                                (Get-Process -Name $serverCfg.process -ErrorAction SilentlyContinue)) {
                                try {
                                    $installDir = Get-ServerInstallDir -SteamRoot $Cfg.steamcmd_root -AppId $serverCfg.appid
                                    $settingsIni = Join-Path $installDir "Pal\Saved\Config\WindowsServer\PalWorldSettings.ini"
                                    $password = Get-PalworldAdminPassword -SettingsIni $settingsIni
                                    Invoke-Rcon -RconHost $serverCfg.rcon.host -Port $serverCfg.rcon.port -Password $password -Command "Save" | Out-Null
                                    Start-Sleep -Seconds 3
                                } catch {
                                    Write-HephLog -LogPath $LogPath -Message "[$($serverCfg.name)] Save RCON pre-backup echoue (backup quand meme): $($_.Exception.Message)"
                                }
                            }
                            $file = Backup-GameSave -Cfg $Cfg -ServerCfg $serverCfg -Kind "manual"
                            $result = [pscustomobject]@{ ok = $true; detail = "backup ${file} cree" }
                        }
                        "restore_save" {
                            $detail = Restore-GameSave -Cfg $Cfg -ServerCfg $serverCfg -BackupFile $order.backup_file
                            $result = [pscustomobject]@{ ok = $true; detail = $detail }
                        }
                        "list_files" {
                            $result = Invoke-ListFiles -Cfg $Cfg -ServerCfg $serverCfg -Root $order.root
                        }
                        "read_file" {
                            $result = Invoke-ReadFile -Cfg $Cfg -ServerCfg $serverCfg -Root $order.root -Path $order.path
                        }
                        "write_file" {
                            $result = Invoke-WriteFile -Cfg $Cfg -ServerCfg $serverCfg -Root $order.root `
                                -Path $order.path -ContentB64 $order.content_b64 -ExpectedSha256 $order.expected_sha256
                        }
                        default {
                            $result = [pscustomobject]@{ ok = $false; detail = "type d'ordre inconnu: $($order.type)" }
                        }
                    }
                } catch {
                    $result = [pscustomobject]@{ ok = $false; detail = "exception: $($_.Exception.Message)" }
                }

                $finalStatus = if ($result.ok) { "done" } else { "failed" }
                $extra = $null
                if ($order.type -eq "list_files" -and $result.PSObject.Properties.Name -contains "files") {
                    $extra = @{ files = @($result.files) }
                } elseif ($order.type -eq "read_file" -and $result.PSObject.Properties.Name -contains "content_b64") {
                    $extra = @{ content_b64 = $result.content_b64; sha256 = $result.sha256 }
                }
                Set-HephOrderStatus -Cfg $Cfg -OrderId $order.id -Status $finalStatus -Detail $result.detail -Extra $extra -LogPath $LogPath | Out-Null
                Write-HephLog -LogPath $LogPath -Message "ordre $($order.id) [$($order.type)/$($serverCfg.name)]: ${finalStatus} -- $($result.detail)"
            }
        } catch {
            Write-HephLog -LogPath $LogPath -Message "ordre $($order.id): ERREUR traitement: $($_.Exception.Message)"
        }

        # Re-rapport d'etat immediat : l'UI voit le resultat reel de l'ordre en
        # secondes au lieu d'attendre le tick planifie suivant.
        Send-HephStateReport -Cfg $Cfg -LogPath $LogPath | Out-Null
    }

    # Decision d'auto-update jeu rapatriee cote backend (app/game_updates.py,
    # 17/07/2026) : le backend cree desormais un ordre "update" quand il juge
    # eligible (buildid diff, 0 joueur, cooldown), et cet ordre est draine comme
    # n'importe quel ordre manuel par la boucle ci-dessus. Get-PublicBuildId reste
    # utilise plus haut pour le push Kuma MAJ (kuma_maj_push) ; auto_update_window/
    # Test-InAutoUpdateWindow restent pour le backup quotidien ci-dessous.

    # Backup quotidien des saves pendant la fenetre d'auto-update : au plus un par
    # 20h par serveur (les cycles de 2 min repassent plusieurs fois dans la fenetre).
    if (Test-InAutoUpdateWindow -Window $Cfg.auto_update_window -Now $Now) {
        foreach ($serverCfg in $Cfg.servers) {
            try {
                $saveDir = Get-GameSaveDir -Cfg $Cfg -ServerCfg $serverCfg
                if (-not $saveDir -or -not (Test-Path -LiteralPath $saveDir)) {
                    continue
                }
                $backups = @(Get-GameSaveBackups -Cfg $Cfg -ServerCfg $serverCfg)
                $needDaily = $true
                if ($backups.Count -gt 0) {
                    $newest = ([datetime]::Parse($backups[0].created)).ToUniversalTime()
                    if (((Get-Date).ToUniversalTime() - $newest).TotalHours -lt 20) {
                        $needDaily = $false
                    }
                }
                if ($needDaily) {
                    $file = Backup-GameSave -Cfg $Cfg -ServerCfg $serverCfg -Kind "daily"
                    Write-HephLog -LogPath $LogPath -Message "[BACKUP] [$($serverCfg.name)] backup quotidien ${file}"
                }
            } catch {
                Write-HephLog -LogPath $LogPath -Message "[BACKUP] [$($serverCfg.name)] ERREUR backup quotidien: $($_.Exception.Message)"
            }
        }
    }

    Write-HephLog -LogPath $LogPath -Message "=== cycle termine ==="
}

function Test-HephLockFree {
    <#
    .SYNOPSIS
        Vrai si le fichier de verrou est absent, ou present mais plus vieux que MaxAgeMinutes.
    .NOTES
        Se base sur LastWriteTime plutot que sur un contenu horodate a parser -- le seul
        contenu du fichier sert de trace pour le diagnostic humain, pas de source de verite.
    #>
    param(
        [Parameter(Mandatory)]
        [string]$LockPath,

        [int]$MaxAgeMinutes = 10,

        [datetime]$Now = (Get-Date)
    )

    if (-not (Test-Path -LiteralPath $LockPath)) {
        return $true
    }

    $age = $Now - (Get-Item -LiteralPath $LockPath).LastWriteTime
    return ($age.TotalMinutes -ge $MaxAgeMinutes)
}

function Test-SteamcmdRunning {
    <#
    .SYNOPSIS
        Vrai si un process steamcmd tourne actuellement sur la machine.
    .NOTES
        Sert a distinguer "cycle mort" (crash agent, verrou orphelin) de "cycle lent"
        (grosse MAJ steamcmd qui depasse l'age max du verrou) avant de voler un verrou
        perime. Fonction separee pour etre mockable dans les tests Pester.
    #>
    return [bool](Get-Process -Name "steamcmd" -ErrorAction SilentlyContinue)
}

function Invoke-HephAgentCycleLocked {
    <#
    .SYNOPSIS
        Enrobe Invoke-HephAgentCycle d'un verrou fichier local anti-recouvrement.
    .NOTES
        Incident evite : la tache planifiee relance le script toutes les 2 min ; un
        Update-GameServer (steamcmd) dure souvent plus longtemps. Sans verrou, un second
        cycle recupererait le meme ordre "running" (pending_orders() renvoie aussi les
        ordres running, et running->running n'est pas un 409) et relancerait
        Update-GameServer EN PARALLELE sur le meme serveur (stop/start/steamcmd concurrents).

        Verrou fichier simple (pas de Mutex .NET global) : suffisant pour une tache
        planifiee mono-machine. Age max par defaut 10 min : largement au-dessus du cycle de
        2 min, en dessous d'une mise a jour qui trainerait anormalement (auquel cas le
        cycle suivant reprend la main plutot que de rester bloque indefiniment sur un
        verrou orphelin, ex. crash du process agent en plein cycle).

        Si le verrou est present et recent : on ne touche NI aux ordres NI a l'etat -- le
        skip est total, pas partiel.

        Le verrou est supprime dans un `finally` : garanti meme si Invoke-HephAgentCycle
        leve une exception non geree (ex. Write-HephLog qui echoue) -- l'exception continue
        de se propager APRES le nettoyage, elle n'est pas avalee ici.
    #>
    param(
        [Parameter(Mandatory)]
        $Cfg,

        [datetime]$Now = (Get-Date),

        [string]$LogPath = "C:\hephaestos\hephaestos-agent.log",

        [Parameter(Mandatory)]
        [string]$LockPath,

        [int]$LockMaxAgeMinutes = 10,

        [string]$ConfigPath = (Join-Path $PSScriptRoot "hephaestos-config.json")
    )

    if (-not (Test-HephLockFree -LockPath $LockPath -MaxAgeMinutes $LockMaxAgeMinutes -Now $Now)) {
        Write-HephLog -LogPath $LogPath -Message "cycle precedent encore actif (lock ${LockPath}), skip"
        return
    }

    # Verrou perime mais steamcmd encore actif = cycle LENT (grosse MAJ), pas mort :
    # voler le verrou relancerait steamcmd en parallele sur le meme serveur --
    # exactement l'incident que le verrou existe pour empecher. On skippe, le tick
    # suivant re-evaluera (des que steamcmd se termine, le vol redevient possible).
    if ((Test-Path -LiteralPath $LockPath) -and (Test-SteamcmdRunning)) {
        Write-HephLog -LogPath $LogPath -Message "verrou perime (${LockPath}) mais steamcmd encore actif : cycle lent presume, skip sans vol de verrou"
        return
    }

    $lockDir = Split-Path -Path $LockPath -Parent
    if ($lockDir -and -not (Test-Path -LiteralPath $lockDir)) {
        New-Item -ItemType Directory -Path $lockDir -Force | Out-Null
    }
    Set-Content -LiteralPath $LockPath -Value $Now.ToString("o") -Force

    try {
        Invoke-HephAgentCycle -Cfg $Cfg -Now $Now -LogPath $LogPath -LockPath $LockPath -ConfigPath $ConfigPath
    } finally {
        Remove-Item -LiteralPath $LockPath -ErrorAction SilentlyContinue
    }
}

if ($MyInvocation.InvocationName -ne ".") {
    $cfg = Get-HephConfig -Path $ConfigPath
    $lockPath = Join-Path $PSScriptRoot "hephaestos-agent.lock"
    Invoke-HephAgentCycleLocked -Cfg $cfg -Now $Now -LogPath $LogPath -LockPath $lockPath -ConfigPath $ConfigPath
}
