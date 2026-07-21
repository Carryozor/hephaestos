Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot/../hephaestos-lib.ps1"
    . "$PSScriptRoot/../hephaestos-agent.ps1"

    # Racine SteamCMD de test partagee par tous les cycles de ce fichier : un vrai
    # manifest (installdir + buildid) sous $TestDrive, pour que Get-ManifestPath /
    # Get-LocalBuildId fonctionnent reellement dans les tests d'integration du cycle
    # complet (plus de install_dir/manifest codes en dur par serveur).
    $script:agentTestSteamRoot = Join-Path $TestDrive "agent-steam"
    $script:agentTestManifestDir = Join-Path $script:agentTestSteamRoot "steamapps"
    New-Item -ItemType Directory -Path $script:agentTestManifestDir -Force | Out-Null
    @'
"AppState"
{
	"appid"		"2394010"
	"installdir"		"PalServer"
	"buildid"		"100"
}
'@ | Set-Content -LiteralPath (Join-Path $script:agentTestManifestDir "appmanifest_2394010.acf") -Encoding UTF8

    function New-TestCfg {
    param([string]$KumaMajPush = $null)

    $server = [pscustomobject]@{
        name         = "palworld"
        appid        = 2394010
        process      = "PalServer-Win64-Shipping-Cmd"
        start_task   = "PalServer"
        stop_adapter = "palworld-rcon"
        rcon         = [pscustomobject]@{ host = "127.0.0.1"; port = 25575 }
    }
    if ($KumaMajPush) {
        $server | Add-Member -NotePropertyName kuma_maj_push -NotePropertyValue $KumaMajPush
    }

    return [pscustomobject]@{
        api_base           = "http://127.0.0.1:8710"
        agent_token        = "tok"
        kuma_agent_push    = "https://kuma/push/agent"
        steamcmd           = "C:\steam\steamcmd.exe"
        steamcmd_root      = $script:agentTestSteamRoot
        auto_update_window = [pscustomobject]@{ start = "05:00"; end = "05:30" }
        servers            = @($server)
    }
    }
}

Describe "Invoke-HephAgentCycle -- ordres" {
    BeforeEach {
        $script:cfg = New-TestCfg
        $script:calls = @()

        Mock Get-LocalBuildId { "100" }
        Mock Get-Process { $null }
        Mock Get-PalworldPlayers { [pscustomobject]@{ Count = 0; Players = @() } }
        Mock Get-PublicBuildId { "100" }
        Mock Send-KumaPush {}

        Mock Invoke-HephApi {
            $script:calls += [pscustomobject]@{ Method = $Method; Path = $Path; Body = $Body }
            if ($Path -eq "/api/agent/orders") {
                return [pscustomobject]@{ orders = @($script:pendingOrder) }
            }
            return [pscustomobject]@{ ok = $true }
        }
    }

    It "un ordre update pending : passe running puis done, dans cet ordre, avec le detail de Update-GameServer" {
        $script:pendingOrder = [pscustomobject]@{ id = "o1"; server = "palworld"; type = "update"; status = "pending"; created = "x"; detail = $null }
        Mock Update-GameServer { [pscustomobject]@{ ok = $true; detail = "mise a jour reussie : 100 -> 101" } }

        Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log")

        $orderCalls = $script:calls | Where-Object { $_.Path -eq "/api/agent/orders/o1" }
        $orderCalls.Count | Should -Be 2
        $orderCalls[0].Body.status | Should -Be "running"
        $orderCalls[1].Body.status | Should -Be "done"
        $orderCalls[1].Body.detail | Should -Be "mise a jour reussie : 100 -> 101"
    }

    It "echec de Update-GameServer : rapporte failed avec le detail exact" {
        $script:pendingOrder = [pscustomobject]@{ id = "o2"; server = "palworld"; type = "update"; status = "pending"; created = "x"; detail = $null }
        Mock Update-GameServer { [pscustomobject]@{ ok = $false; detail = "steamcmd a echoue" } }

        Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log")

        $orderCalls = $script:calls | Where-Object { $_.Path -eq "/api/agent/orders/o2" }
        $orderCalls[0].Body.status | Should -Be "running"
        $orderCalls[1].Body.status | Should -Be "failed"
        $orderCalls[1].Body.detail | Should -Be "steamcmd a echoue"
    }

    It "exception non geree pendant Update-GameServer : rapporte quand meme failed (ne remonte pas)" {
        $script:pendingOrder = [pscustomobject]@{ id = "o3"; server = "palworld"; type = "update"; status = "pending"; created = "x"; detail = $null }
        Mock Update-GameServer { throw "boom inattendu" }

        { Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log") } | Should -Not -Throw

        $orderCalls = $script:calls | Where-Object { $_.Path -eq "/api/agent/orders/o3" }
        $orderCalls[1].Body.status | Should -Be "failed"
        $orderCalls[1].Body.detail | Should -Match "boom inattendu"
    }

    It "ordre restart : appelle Restart-GameServer (pas Update-GameServer) puis rapporte done" {
        $script:pendingOrder = [pscustomobject]@{ id = "o4"; server = "palworld"; type = "restart"; status = "pending"; created = "x"; detail = $null }
        Mock Update-GameServer {}
        Mock Restart-GameServer {}

        Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log")

        Should -Invoke Restart-GameServer -Times 1
        Should -Invoke Update-GameServer -Times 0
        $orderCalls = $script:calls | Where-Object { $_.Path -eq "/api/agent/orders/o4" }
        $orderCalls[1].Body.status | Should -Be "done"
    }

    It "ordre start : appelle Start-GameServer -ServerCfg puis rapporte done" {
        $script:pendingOrder = [pscustomobject]@{ id = "o7"; server = "palworld"; type = "start"; status = "pending"; created = "x"; detail = $null }
        Mock Update-GameServer {}
        Mock Start-GameServer {}

        Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log")

        Should -Invoke Start-GameServer -Times 1 -ParameterFilter { $ServerCfg.name -eq "palworld" }
        Should -Invoke Update-GameServer -Times 0
        $orderCalls = $script:calls | Where-Object { $_.Path -eq "/api/agent/orders/o7" }
        $orderCalls[1].Body.status | Should -Be "done"
    }

    It "ordre stop : appelle Stop-GameServer -Cfg -ServerCfg puis rapporte done" {
        $script:pendingOrder = [pscustomobject]@{ id = "o8"; server = "palworld"; type = "stop"; status = "pending"; created = "x"; detail = $null }
        Mock Update-GameServer {}
        Mock Stop-GameServer {}

        Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log")

        Should -Invoke Stop-GameServer -Times 1 -ParameterFilter { $Cfg -eq $script:cfg -and $ServerCfg.name -eq "palworld" }
        Should -Invoke Update-GameServer -Times 0
        $orderCalls = $script:calls | Where-Object { $_.Path -eq "/api/agent/orders/o8" }
        $orderCalls[1].Body.status | Should -Be "done"
    }

    It "409 sur le passage a running (ordre deja termine ailleurs) : n'execute PAS Update-GameServer" {
        $script:pendingOrder = [pscustomobject]@{ id = "o5"; server = "palworld"; type = "update"; status = "pending"; created = "x"; detail = $null }
        Mock Update-GameServer {}
        Mock Invoke-HephApi {
            $script:calls += [pscustomobject]@{ Method = $Method; Path = $Path; Body = $Body }
            if ($Path -eq "/api/agent/orders") {
                return [pscustomobject]@{ orders = @($script:pendingOrder) }
            }
            if ($Path -eq "/api/agent/orders/o5" -and $Body.status -eq "running") {
                throw "Echec appel API Hephaestos POST /api/agent/orders/o5: Response status code does not indicate success: 409 (Conflict)."
            }
            return [pscustomobject]@{ ok = $true }
        }

        { Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log") } | Should -Not -Throw

        Should -Invoke Update-GameServer -Times 0
    }

    It "ordre pour un serveur inconnu de la config : ignore proprement, pas d'exception" {
        $script:pendingOrder = [pscustomobject]@{ id = "o6"; server = "inconnu"; type = "update"; status = "pending"; created = "x"; detail = $null }
        Mock Update-GameServer {}

        { Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log") } | Should -Not -Throw

        Should -Invoke Update-GameServer -Times 0
    }
}

Describe "Invoke-HephAgentCycle -- drainage des ordres" {
    BeforeEach {
        $script:cfg = New-TestCfg
        $script:calls = @()

        Mock Get-LocalBuildId { "100" }
        Mock Get-Process { $null }
        Mock Get-PalworldPlayers { [pscustomobject]@{ Count = 0; Players = @() } }
        Mock Get-PublicBuildId { "100" }
        Mock Send-KumaPush {}

        # File d'ordres mutable simulant le backend : GET /orders renvoie les
        # ordres non terminaux ; un POST de statut met a jour l'ordre en place
        # (comme le vrai backend). Chaque test peuple $script:orderQueue.
        Mock Invoke-HephApi {
            $script:calls += [pscustomobject]@{ Method = $Method; Path = $Path; Body = $Body }
            if ($Path -eq "/api/agent/orders") {
                return [pscustomobject]@{ orders = @($script:orderQueue | Where-Object { $_.status -in @("pending", "running") }) }
            }
            if ($Path -match "^/api/agent/orders/(.+)$") {
                $target = $script:orderQueue | Where-Object { $_.id -eq $Matches[1] } | Select-Object -First 1
                if ($target) { $target.status = $Body.status }
                return [pscustomobject]@{ ok = $true }
            }
            return [pscustomobject]@{ ok = $true }
        }
    }

    It "deux ordres pending : les DEUX sont executes dans le meme cycle (running puis done chacun)" {
        $script:orderQueue = @(
            [pscustomobject]@{ id = "d1"; server = "palworld"; type = "restart"; status = "pending"; created = "x"; detail = $null },
            [pscustomobject]@{ id = "d2"; server = "palworld"; type = "update"; status = "pending"; created = "x"; detail = $null }
        )
        Mock Restart-GameServer {}
        Mock Update-GameServer { [pscustomobject]@{ ok = $true; detail = "ok" } }

        Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log")

        Should -Invoke Restart-GameServer -Times 1
        Should -Invoke Update-GameServer -Times 1
        foreach ($id in @("d1", "d2")) {
            $orderCalls = @($script:calls | Where-Object { $_.Path -eq "/api/agent/orders/$id" })
            $orderCalls.Count | Should -Be 2
            $orderCalls[0].Body.status | Should -Be "running"
            $orderCalls[1].Body.status | Should -Be "done"
        }
    }

    It "l'etat est re-POSTe apres chaque ordre (1 initial + 1 par ordre)" {
        $script:orderQueue = @(
            [pscustomobject]@{ id = "d3"; server = "palworld"; type = "restart"; status = "pending"; created = "x"; detail = $null },
            [pscustomobject]@{ id = "d4"; server = "palworld"; type = "stop"; status = "pending"; created = "x"; detail = $null }
        )
        Mock Restart-GameServer {}
        Mock Stop-GameServer {}

        Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log")

        $stateCalls = @($script:calls | Where-Object { $_.Path -eq "/api/agent/state" })
        $stateCalls.Count | Should -Be 3
    }

    It "ordre annule entre deux iterations (disparu du re-GET) : jamais execute" {
        $script:orderQueue = @(
            [pscustomobject]@{ id = "d5"; server = "palworld"; type = "restart"; status = "pending"; created = "x"; detail = $null },
            [pscustomobject]@{ id = "d6"; server = "palworld"; type = "update"; status = "pending"; created = "x"; detail = $null }
        )
        # L'execution de d5 annule d6 (simule un DELETE utilisateur pendant le cycle).
        Mock Restart-GameServer {
            $d6 = $script:orderQueue | Where-Object { $_.id -eq "d6" }
            $d6.status = "failed"
        }
        Mock Update-GameServer { [pscustomobject]@{ ok = $true; detail = "ok" } }

        Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log")

        Should -Invoke Update-GameServer -Times 0
        @($script:calls | Where-Object { $_.Path -eq "/api/agent/orders/d6" }).Count | Should -Be 0
    }

    It "report de statut final en echec transitoire : l'ordre n'est PAS retente dans le meme cycle" {
        $script:orderQueue = @(
            [pscustomobject]@{ id = "d7"; server = "palworld"; type = "restart"; status = "pending"; created = "x"; detail = $null }
        )
        Mock Restart-GameServer {}
        Mock Invoke-HephApi {
            $script:calls += [pscustomobject]@{ Method = $Method; Path = $Path; Body = $Body }
            if ($Path -eq "/api/agent/orders") {
                return [pscustomobject]@{ orders = @($script:orderQueue | Where-Object { $_.status -in @("pending", "running") }) }
            }
            if ($Path -eq "/api/agent/orders/d7") {
                if ($Body.status -eq "done") { throw "Echec appel API Hephaestos POST: 500" }
                $script:orderQueue[0].status = $Body.status
                return [pscustomobject]@{ ok = $true }
            }
            return [pscustomobject]@{ ok = $true }
        }

        { Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log") } | Should -Not -Throw

        # Une seule tentative : un seul passage a running, une seule execution.
        Should -Invoke Restart-GameServer -Times 1
        $runningCalls = @($script:calls | Where-Object { $_.Path -eq "/api/agent/orders/d7" -and $_.Body.status -eq "running" })
        $runningCalls.Count | Should -Be 1
    }

    It "le verrou est rafraichi avant chaque ordre quand -LockPath est fourni" {
        $script:orderQueue = @(
            [pscustomobject]@{ id = "d8"; server = "palworld"; type = "restart"; status = "pending"; created = "x"; detail = $null }
        )
        Mock Restart-GameServer {}
        $lockPath = Join-Path $TestDrive "drain.lock"
        Set-Content -LiteralPath $lockPath -Value "ancien"

        Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log") -LockPath $lockPath

        (Get-Content -LiteralPath $lockPath -Raw).Trim() | Should -Not -Be "ancien"
    }

    It "traite un ordre install_game sans entree dans Cfg.servers et rapporte les candidats" {
        # Un serveur en installing n'est PAS dans la config poussee ($script:cfg n'a que
        # "palworld") : le dispatch doit executer l'ordre AVANT le lookup Cfg.servers
        # (sinon "serveur inconnu de la config, ignore").
        $script:orderQueue = @(
            [pscustomobject]@{ id = "d9"; server = "vrising"; type = "install_game"; status = "pending"; created = "x"; detail = $null; appid = 1829350 }
        )
        Mock Invoke-InstallGame { [pscustomobject]@{ ok = $true; detail = "installe"; exe_candidates = @("VRisingServer.exe") } }

        Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log")

        Should -Invoke Invoke-InstallGame -Times 1 -ParameterFilter { $AppId -eq 1829350 }
        $doneCalls = @($script:calls | Where-Object { $_.Path -eq "/api/agent/orders/d9" -and $_.Body.status -eq "done" })
        $doneCalls.Count | Should -Be 1
        (@($doneCalls[0].Body.exe_candidates))[0] | Should -Be "VRisingServer.exe"
    }

    It "traite un ordre list_files via le dispatch normal (lookup Cfg.servers) et rapporte les fichiers" {
        # Contraste volontaire avec install_game (Lot 2) : list_files/read_file/write_file
        # ne concernent que des serveurs deja actifs, donc "palworld" (deja dans
        # $script:cfg.servers via New-TestCfg), pas un serveur inconnu de la config.
        $script:orderQueue = @(
            [pscustomobject]@{ id = "d10"; server = "palworld"; type = "list_files"; status = "pending"; created = "x"; detail = $null; root = "install" }
        )
        Mock Invoke-ListFiles { [pscustomobject]@{ ok = $true; detail = "1 fichier(s)"; files = @("a.ini") } }

        Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log")

        Should -Invoke Invoke-ListFiles -Times 1 -ParameterFilter { $Root -eq "install" }
        $doneCalls = @($script:calls | Where-Object { $_.Path -eq "/api/agent/orders/d10" -and $_.Body.status -eq "done" })
        $doneCalls.Count | Should -Be 1
        (@($doneCalls[0].Body.files))[0] | Should -Be "a.ini"
    }
}

Describe "Invoke-HephAgentCycle -- etat + kuma" {
    BeforeEach {
        $script:cfg = New-TestCfg -KumaMajPush "https://kuma/push/maj"
        $script:apiCalls = @()
        $script:kumaCalls = @()

        Mock Get-Process { [pscustomobject]@{ Id = 1; StartTime = (Get-Date "2026-07-13 06:00") } }
        Mock Get-PalworldPlayers { [pscustomobject]@{ Count = 3; Players = @() } }
        Mock Invoke-HephApi {
            $script:apiCalls += [pscustomobject]@{ Method = $Method; Path = $Path; Body = $Body }
            if ($Path -eq "/api/agent/orders") { return [pscustomobject]@{ orders = @() } }
            return [pscustomobject]@{ ok = $true }
        }
        Mock Send-KumaPush {
            $script:kumaCalls += [pscustomobject]@{ PushUrl = $PushUrl; Status = $Status; Msg = $Msg }
        }
    }

    It "poste l'etat complet (buildid, process_up, players) sans champ superflu" {
        Mock Get-LocalBuildId { "100" }
        Mock Get-PublicBuildId { "100" }

        Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log")

        $stateCall = $script:apiCalls | Where-Object { $_.Path -eq "/api/agent/state" }
        $stateCall.Count | Should -Be 1
        $srv = $stateCall.Body.servers.palworld
        $srv.buildid | Should -Be "100"
        $srv.process_up | Should -Be $true
        $srv.players | Should -Be 3
        (($srv.Keys) | Sort-Object) -join "," | Should -Be "buildid,installed_mod_ids,players,players_list,process_cpu_percent,process_mem_mb,process_started_at,process_up,rcon_info,save_backups"
    }

    It "inclut rcon_info, process_cpu_percent et process_mem_mb dans l'etat rapporte pour un serveur up" {
        Mock Get-LocalBuildId { "100" }
        Mock Get-PublicBuildId { "100" }
        Mock Get-ServerRconInfo { "Welcome to Pal Server[Version:0.1.2] MyWorld" }
        Mock Get-ProcessMetrics { [pscustomobject]@{ CpuPercent = 3.2; MemMb = 812.0 } }

        Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log")

        $stateCall = $script:apiCalls | Where-Object { $_.Path -eq "/api/agent/state" }
        $srv = $stateCall.Body.servers.palworld
        $srv.rcon_info | Should -Be "Welcome to Pal Server[Version:0.1.2] MyWorld"
        $srv.process_cpu_percent | Should -Be 3.2
        $srv.process_mem_mb | Should -Be 812.0
    }

    It "buildid local = public : push kuma_maj_push en up avec le build dans le message" {
        Mock Get-LocalBuildId { "100" }
        Mock Get-PublicBuildId { "100" }

        Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log")

        $majCall = $script:kumaCalls | Where-Object { $_.PushUrl -eq "https://kuma/push/maj" }
        $majCall.Status | Should -Be "up"
        $majCall.Msg | Should -Match "100"
    }

    It "buildid local != public : push kuma_maj_push en down avec les deux builds dans le message" {
        Mock Get-LocalBuildId { "100" }
        Mock Get-PublicBuildId { "101" }

        Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log")

        $majCall = $script:kumaCalls | Where-Object { $_.PushUrl -eq "https://kuma/push/maj" }
        $majCall.Status | Should -Be "down"
        $majCall.Msg | Should -Match "100"
        $majCall.Msg | Should -Match "101"
    }

    It "push toujours kuma_agent_push en up (heartbeat de vie de l'agent)" {
        Mock Get-LocalBuildId { "100" }
        Mock Get-PublicBuildId { "100" }

        Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log")

        $agentCall = $script:kumaCalls | Where-Object { $_.PushUrl -eq "https://kuma/push/agent" }
        $agentCall.Status | Should -Be "up"
    }

    It "erreur sur un serveur (buildid local illisible) n'empeche pas la suite du cycle (etat + ordres traites quand meme)" {
        Mock Get-LocalBuildId { throw "manifest introuvable" }
        Mock Get-PublicBuildId { "100" }

        { Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent.log") } | Should -Not -Throw

        $stateCall = $script:apiCalls | Where-Object { $_.Path -eq "/api/agent/state" }
        $stateCall.Count | Should -Be 1
        $stateCall.Body.servers.palworld.buildid | Should -Be $null
    }

    It "echec Send-KumaPush avec un token dans l'URL : le log ne contient PAS le token en clair" {
        Mock Get-LocalBuildId { "100" }
        Mock Get-PublicBuildId { "100" }
        Mock Send-KumaPush { throw "Echec push Kuma vers https://kuma/api/push/SECRETTOKEN123: connection refused" }
        $logPath = Join-Path $TestDrive "kuma-mask.log"

        Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date "2026-07-13 12:00") -LogPath $logPath

        $logContent = Get-Content -LiteralPath $logPath -Raw
        $logContent | Should -Not -Match "SECRETTOKEN123"
        $logContent | Should -Match "/api/push/\*\*\*"
    }
}

Describe "Invoke-HephAgentCycle -- rapport d'etat enrichi et config backend (v2.1.0)" {
    BeforeEach {
        $script:configPathCycle = Join-Path $TestDrive "hephaestos-config-cycle-$([guid]::NewGuid()).json"
        (New-TestCfg) | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $script:configPathCycle
        $script:cfgCycle = Get-HephConfig -Path $script:configPathCycle
        $script:apiCallsCycle = @()

        Mock Get-Process { $null }
        Mock Get-LocalBuildId { "100" }
        Mock Get-PublicBuildId { "100" }
        Mock Send-KumaPush {}
    }

    It "poste agent_version et config_servers dans le rapport d'etat" {
        Mock Invoke-HephApi {
            $script:apiCallsCycle += [pscustomobject]@{ Method = $Method; Path = $Path; Body = $Body }
            if ($Path -eq "/api/agent/orders") { return [pscustomobject]@{ orders = @($null) } }
            return [pscustomobject]@{ ok = $true }
        }

        Invoke-HephAgentCycle -Cfg $script:cfgCycle -ConfigPath $script:configPathCycle -Now (Get-Date "2026-07-18 12:00") -LogPath (Join-Path $TestDrive "cycle-enrichi.log")

        $stateCall = $script:apiCallsCycle | Where-Object { $_.Path -eq "/api/agent/state" } | Select-Object -First 1
        $stateCall.Body.agent_version | Should -Be "2.2.0"
        @($stateCall.Body.config_servers).Count | Should -Be 1
        $stateCall.Body.config_servers[0].name | Should -Be "palworld"
    }

    It "poste discovered_games (jeu installe absent de la config), sans toucher au disque" {
        @'
"AppState"
{
	"appid"		"896660"
	"name"		"Valheim dedicated"
	"buildid"		"55"
	"installdir"		"valheim"
}
'@ | Set-Content -LiteralPath (Join-Path $script:agentTestManifestDir "appmanifest_896660.acf") -Encoding UTF8

        Mock Invoke-HephApi {
            $script:apiCallsCycle += [pscustomobject]@{ Method = $Method; Path = $Path; Body = $Body }
            if ($Path -eq "/api/agent/orders") { return [pscustomobject]@{ orders = @($null) } }
            return [pscustomobject]@{ ok = $true }
        }
        $before = (Get-Item -LiteralPath $script:configPathCycle).LastWriteTimeUtc

        Invoke-HephAgentCycle -Cfg $script:cfgCycle -ConfigPath $script:configPathCycle -Now (Get-Date "2026-07-18 12:00") -LogPath (Join-Path $TestDrive "cycle-discover.log")

        $stateCall = $script:apiCallsCycle | Where-Object { $_.Path -eq "/api/agent/state" } | Select-Object -First 1
        @($stateCall.Body.discovered_games).Count | Should -Be 1
        $stateCall.Body.discovered_games[0].appid | Should -Be 896660
        (Get-Item -LiteralPath $script:configPathCycle).LastWriteTimeUtc | Should -Be $before
    }

    It "applique et persiste la config poussee par le backend (bloc config du GET orders) -- l'ordre POST state puis GET orders est preserve" {
        Mock Invoke-HephApi {
            $script:apiCallsCycle += [pscustomobject]@{ Method = $Method; Path = $Path; Body = $Body }
            if ($Path -eq "/api/agent/orders") {
                return [pscustomobject]@{
                    orders = @($null)
                    config = [pscustomobject]@{
                        hash    = "hbackend1"
                        servers = @([pscustomobject]@{ name = "palworld"; appid = 2394010; launch_args = "-new" })
                    }
                }
            }
            return [pscustomobject]@{ ok = $true }
        }

        Invoke-HephAgentCycle -Cfg $script:cfgCycle -ConfigPath $script:configPathCycle -Now (Get-Date "2026-07-18 12:00") -LogPath (Join-Path $TestDrive "cycle-backend-config.log")

        $reloaded = Get-HephConfig -Path $script:configPathCycle
        $reloaded.backend_config_hash | Should -Be "hbackend1"
        $reloaded.servers[0].launch_args | Should -Be "-new"

        # invariant d'ordre : le premier POST /api/agent/state du cycle precede le GET
        # /api/agent/orders qui a livre ce bloc config.
        $firstStateIndex = [array]::IndexOf(@($script:apiCallsCycle.Path), "/api/agent/state")
        $firstOrdersIndex = [array]::IndexOf(@($script:apiCallsCycle.Path), "/api/agent/orders")
        $firstStateIndex | Should -BeLessThan $firstOrdersIndex
    }

    It "poste config_hash quand backend_config_hash est deja present sur la config" {
        $script:cfgCycle | Add-Member -NotePropertyName backend_config_hash -NotePropertyValue "hexisting"
        Mock Invoke-HephApi {
            $script:apiCallsCycle += [pscustomobject]@{ Method = $Method; Path = $Path; Body = $Body }
            if ($Path -eq "/api/agent/orders") { return [pscustomobject]@{ orders = @($null) } }
            return [pscustomobject]@{ ok = $true }
        }

        Invoke-HephAgentCycle -Cfg $script:cfgCycle -ConfigPath $script:configPathCycle -Now (Get-Date "2026-07-18 12:00") -LogPath (Join-Path $TestDrive "cycle-hash.log")

        $stateCall = $script:apiCallsCycle | Where-Object { $_.Path -eq "/api/agent/state" } | Select-Object -First 1
        $stateCall.Body.config_hash | Should -Be "hexisting"
    }

    It "ne leve pas d'exception et ne casse pas le drainage si le GET orders ne renvoie pas de bloc config (retro-compat, mock historique)" {
        Mock Invoke-HephApi {
            $script:apiCallsCycle += [pscustomobject]@{ Method = $Method; Path = $Path; Body = $Body }
            if ($Path -eq "/api/agent/orders") { return [pscustomobject]@{ orders = @($null) } }
            return [pscustomobject]@{ ok = $true }
        }

        { Invoke-HephAgentCycle -Cfg $script:cfgCycle -ConfigPath $script:configPathCycle -Now (Get-Date "2026-07-18 12:00") -LogPath (Join-Path $TestDrive "cycle-no-config.log") } | Should -Not -Throw

        $reloaded = Get-HephConfig -Path $script:configPathCycle
        $reloaded.PSObject.Properties.Name | Should -Not -Contain "backend_config_hash"
    }
}

Describe "Invoke-HephAgentCycle -- comptage joueurs Windrose" {
    BeforeEach {
        function New-TestCfgWindrose {
            param([bool]$WindrosePlus = $false, [bool]$WithRcon = $false)

            $server = [pscustomobject]@{
                name         = "windrose"
                appid        = 4129620
                process      = "WindroseServer-Win64-Shipping-Cmd"
                start_task   = "Windrose"
                stop_adapter = "windrose-rcon"
            }
            if ($WithRcon) {
                $server | Add-Member -NotePropertyName rcon -NotePropertyValue ([pscustomobject]@{ host = "127.0.0.1"; port = 25575 })
            }
            if ($WindrosePlus) {
                $server | Add-Member -NotePropertyName windrose_plus -NotePropertyValue $true
            }

            return [pscustomobject]@{
                api_base           = "http://127.0.0.1:8710"
                agent_token        = "tok"
                kuma_agent_push    = "https://kuma/push/agent"
                steamcmd           = "C:\steam\steamcmd.exe"
                steamcmd_root      = $script:agentTestSteamRoot
                auto_update_window = [pscustomobject]@{ start = "05:00"; end = "05:30" }
                servers            = @($server)
            }
        }

        Mock Get-LocalBuildId { "100" }
        Mock Get-PublicBuildId { "100" }
        Mock Get-Process { $null }
        Mock Send-KumaPush {}
        $script:apiCallsWindrose = @()
        Mock Invoke-HephApi {
            $script:apiCallsWindrose += [pscustomobject]@{ Method = $Method; Path = $Path; Body = $Body }
            if ($Path -eq "/api/agent/orders") { return [pscustomobject]@{ orders = @() } }
            return [pscustomobject]@{ ok = $true }
        }
    }

    It "serveur avec windrose_plus:`$true : appelle Get-WindrosePlayers et range le compte dans players" {
        $cfg = New-TestCfgWindrose -WindrosePlus $true
        Mock Get-WindrosePlayers { [pscustomobject]@{ Count = 4; Players = @([pscustomobject]@{ name = "Alice"; session_id = "player:1"; steamid = $null }) } }

        Invoke-HephAgentCycle -Cfg $cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent-windrose.log")

        Should -Invoke Get-WindrosePlayers -Times 1
        $stateCall = $script:apiCallsWindrose | Where-Object { $_.Path -eq "/api/agent/state" }
        $stateCall.Body.servers.windrose.players | Should -Be 4
        $stateCall.Body.servers.windrose.players_list[0].name | Should -Be "Alice"
    }

    It "serveur sans windrose_plus ni rcon (ex. Valheim) : players reste `$null, pas de regression" {
        $cfg = New-TestCfgWindrose
        Mock Get-WindrosePlayers { [pscustomobject]@{ Count = 4; Players = @() } }

        Invoke-HephAgentCycle -Cfg $cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent-windrose.log")

        Should -Invoke Get-WindrosePlayers -Times 0
        $stateCall = $script:apiCallsWindrose | Where-Object { $_.Path -eq "/api/agent/state" }
        $stateCall.Body.servers.windrose.players | Should -Be $null
    }

    It "exception levee par Get-WindrosePlayers : capturee, le cycle continue (etat + kuma s'executent quand meme)" {
        $cfg = New-TestCfgWindrose -WindrosePlus $true
        Mock Get-WindrosePlayers { throw "fichier illisible" }

        { Invoke-HephAgentCycle -Cfg $cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent-windrose.log") } | Should -Not -Throw

        $stateCall = $script:apiCallsWindrose | Where-Object { $_.Path -eq "/api/agent/state" }
        $stateCall.Count | Should -Be 1
        $stateCall.Body.servers.windrose.players | Should -Be $null
        Should -Invoke Send-KumaPush -Times 1 -ParameterFilter { $PushUrl -eq "https://kuma/push/agent" }
    }

    It "Get-WindrosePlayers renvoie Count `$null (fichier de statut indisponible) : players_list reste `$null, pas de purge des sessions suivies" {
        # Regression : si players_list valait @() (liste vide) au lieu de $null dans ce cas,
        # le backend interpreterait ca comme "0 joueur reellement connecte" et purgerait
        # player_sessions -- perdant le suivi first_seen des joueurs toujours connectes
        # pendant un simple glitch de lecture du fichier de statut (pas une vraie deconnexion).
        $cfg = New-TestCfgWindrose -WindrosePlus $true
        Mock Get-WindrosePlayers { [pscustomobject]@{ Count = $null; Players = @() } }

        Invoke-HephAgentCycle -Cfg $cfg -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent-windrose.log")

        $stateCall = $script:apiCallsWindrose | Where-Object { $_.Path -eq "/api/agent/state" }
        $stateCall.Body.servers.windrose.players | Should -Be $null
        $stateCall.Body.servers.windrose.players_list | Should -Be $null
    }
}

Describe "Invoke-HephAgentCycle -- ordres mods" {
    BeforeEach {
        $script:cfgMods = New-TestCfg
        $script:cfgMods.servers[0] | Add-Member -NotePropertyName workshop_appid -NotePropertyValue 1623730 -Force

        Mock Get-Process { [pscustomobject]@{ Id = 1; StartTime = (Get-Date "2026-07-13 04:00") } }
        Mock Get-PalworldPlayers { [pscustomobject]@{ Count = 0; Players = @() } }
        Mock Get-InstalledWorkshopMods { @("123") }
        Mock Send-KumaPush {}

        $script:apiCallsMods = @()
        Mock Invoke-HephApi {
            $script:apiCallsMods += [pscustomobject]@{ Method = $Method; Path = $Path; Body = $Body }
            if ($Path -eq "/api/agent/orders") { return [pscustomobject]@{ orders = @($script:pendingOrderMods) } }
            return [pscustomobject]@{ ok = $true }
        }
    }

    It "traite un ordre install_mod : appelle Install-WorkshopMod puis marque l'ordre done" {
        $script:pendingOrderMods = [pscustomobject]@{ id = "o1"; server = "palworld"; type = "install_mod"; status = "pending"; workshop_id = "3147025543" }
        Mock Install-WorkshopMod {}

        Invoke-HephAgentCycle -Cfg $script:cfgMods -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent-mods.log")

        Should -Invoke Install-WorkshopMod -Times 1 -ParameterFilter { $WorkshopId -eq "3147025543" }
        $orderCall = $script:apiCallsMods | Where-Object { $_.Path -eq "/api/agent/orders/o1" }
        $orderCall[-1].Body.status | Should -Be "done"
    }

    It "traite un ordre remove_mod : appelle Remove-WorkshopMod puis marque l'ordre done" {
        $script:pendingOrderMods = [pscustomobject]@{ id = "o2"; server = "palworld"; type = "remove_mod"; status = "pending"; workshop_id = "3147025543" }
        Mock Remove-WorkshopMod {}

        Invoke-HephAgentCycle -Cfg $script:cfgMods -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent-mods.log")

        Should -Invoke Remove-WorkshopMod -Times 1 -ParameterFilter { $WorkshopId -eq "3147025543" }
        $orderCall = $script:apiCallsMods | Where-Object { $_.Path -eq "/api/agent/orders/o2" }
        $orderCall[-1].Body.status | Should -Be "done"
    }

    It "un echec Install-WorkshopMod marque l'ordre failed avec le message d'exception" {
        $script:pendingOrderMods = [pscustomobject]@{ id = "o3"; server = "palworld"; type = "install_mod"; status = "pending"; workshop_id = "999" }
        Mock Install-WorkshopMod { throw "steamcmd: item introuvable" }

        Invoke-HephAgentCycle -Cfg $script:cfgMods -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent-mods.log")

        $orderCall = $script:apiCallsMods | Where-Object { $_.Path -eq "/api/agent/orders/o3" }
        $orderCall[-1].Body.status | Should -Be "failed"
        $orderCall[-1].Body.detail | Should -Match "steamcmd: item introuvable"
    }

    It "serveur avec workshop_appid configure : remonte installed_mod_ids et process_started_at dans l'etat" {
        $script:pendingOrderMods = $null

        Invoke-HephAgentCycle -Cfg $script:cfgMods -Now (Get-Date "2026-07-13 12:00") -LogPath (Join-Path $TestDrive "agent-mods.log")

        $stateCall = $script:apiCallsMods | Where-Object { $_.Path -eq "/api/agent/state" }
        $stateCall.Body.servers.palworld.installed_mod_ids | Should -Contain "123"
        $stateCall.Body.servers.palworld.process_started_at | Should -Not -BeNullOrEmpty
    }
}

Describe "Write-HephLog" {
    It "cree le fichier et ecrit une ligne horodatee" {
        $logPath = Join-Path $TestDrive "rot.log"

        Write-HephLog -LogPath $logPath -Message "hello"

        Test-Path -LiteralPath $logPath | Should -Be $true
        (Get-Content -LiteralPath $logPath -Raw) | Should -Match "hello"
    }

    It "tronque le fichier avant d'ecrire s'il depasse 1 Mo (rotation)" {
        $logPath = Join-Path $TestDrive "big.log"
        $bigContent = "x" * 1100000
        Set-Content -LiteralPath $logPath -Value $bigContent -NoNewline

        Write-HephLog -LogPath $logPath -Message "apres rotation"

        $content = Get-Content -LiteralPath $logPath -Raw
        $content.Length | Should -BeLessThan 1000
        $content | Should -Match "apres rotation"
    }

    It "masque un token /api/push/<token> present dans le message" {
        $logPath = Join-Path $TestDrive "mask-direct.log"

        Write-HephLog -LogPath $logPath -Message "erreur vers https://kuma/api/push/ABCDEF123456?status=up"

        $content = Get-Content -LiteralPath $logPath -Raw
        $content | Should -Not -Match "ABCDEF123456"
        $content | Should -Match "/api/push/\*\*\*"
    }

    It "utilise l'heure reelle au moment de l'ecriture (pas un horodatage fige) : deux appels espaces produisent deux timestamps differents" {
        $logPath = Join-Path $TestDrive "ts.log"
        $script:tsCallCount = 0
        Mock Get-Date {
            $script:tsCallCount++
            if ($script:tsCallCount -eq 1) { return [datetime]"2026-07-13 10:00:00" }
            return [datetime]"2026-07-13 10:05:00"
        }

        Write-HephLog -LogPath $logPath -Message "premier"
        Write-HephLog -LogPath $logPath -Message "second"

        $lines = @(Get-Content -LiteralPath $logPath)
        $lines[0] | Should -Match "10:00:00"
        $lines[1] | Should -Match "10:05:00"
        $lines[0] | Should -Not -Be $lines[1]
    }
}

Describe "Invoke-HephAgentCycleLocked -- verrou anti-recouvrement" {
    BeforeEach {
        $script:cfg = New-TestCfg
        Mock Invoke-HephAgentCycle {}
    }

    It "lock recent present : n'execute PAS le cycle, logue le skip, laisse le lock en place" {
        $lockPath = Join-Path $TestDrive "recent.lock"
        Set-Content -LiteralPath $lockPath -Value "recent"
        $logPath = Join-Path $TestDrive "locked1.log"

        Invoke-HephAgentCycleLocked -Cfg $script:cfg -Now (Get-Date) -LogPath $logPath -LockPath $lockPath -LockMaxAgeMinutes 10

        Should -Invoke Invoke-HephAgentCycle -Times 0
        (Get-Content -LiteralPath $logPath -Raw) | Should -Match "skip"
        Test-Path -LiteralPath $lockPath | Should -Be $true
    }

    It "lock absent : execute le cycle normalement et supprime le lock a la fin" {
        $lockPath = Join-Path $TestDrive "absent.lock"
        $logPath = Join-Path $TestDrive "locked2.log"

        Invoke-HephAgentCycleLocked -Cfg $script:cfg -Now (Get-Date) -LogPath $logPath -LockPath $lockPath -LockMaxAgeMinutes 10

        Should -Invoke Invoke-HephAgentCycle -Times 1
        Test-Path -LiteralPath $lockPath | Should -Be $false
    }

    It "lock trop vieux (au-dela du seuil) : execute le cycle et supprime le lock a la fin" {
        $lockPath = Join-Path $TestDrive "old.lock"
        Set-Content -LiteralPath $lockPath -Value "old"
        (Get-Item -LiteralPath $lockPath).LastWriteTime = (Get-Date).AddMinutes(-15)
        $logPath = Join-Path $TestDrive "locked3.log"

        Invoke-HephAgentCycleLocked -Cfg $script:cfg -Now (Get-Date) -LogPath $logPath -LockPath $lockPath -LockMaxAgeMinutes 10

        Should -Invoke Invoke-HephAgentCycle -Times 1
        Test-Path -LiteralPath $lockPath | Should -Be $false
    }

    It "exception pendant le cycle : le lock est quand meme supprime (finally)" {
        $lockPath = Join-Path $TestDrive "exc.lock"
        $logPath = Join-Path $TestDrive "locked4.log"
        Mock Invoke-HephAgentCycle { throw "boom mid-cycle" }

        { Invoke-HephAgentCycleLocked -Cfg $script:cfg -Now (Get-Date) -LogPath $logPath -LockPath $lockPath -LockMaxAgeMinutes 10 } | Should -Throw "*boom mid-cycle*"

        Test-Path -LiteralPath $lockPath | Should -Be $false
    }

    It "lock perime MAIS steamcmd encore actif : skip total, pas de vol de verrou" {
        # Un Update-GameServer legitime peut depasser l'age max du verrou (grosse MAJ,
        # reseau lent) : voler le verrou relancerait steamcmd EN PARALLELE sur le meme
        # serveur -- precisement l'incident que le verrou existe pour empecher.
        $lockPath = Join-Path $TestDrive "slow.lock"
        Set-Content -LiteralPath $lockPath -Value "slow"
        (Get-Item -LiteralPath $lockPath).LastWriteTime = (Get-Date).AddMinutes(-15)
        $logPath = Join-Path $TestDrive "locked5.log"
        Mock Test-SteamcmdRunning { $true }

        Invoke-HephAgentCycleLocked -Cfg $script:cfg -Now (Get-Date) -LogPath $logPath -LockPath $lockPath -LockMaxAgeMinutes 10

        Should -Invoke Invoke-HephAgentCycle -Times 0
        (Get-Content -LiteralPath $logPath -Raw) | Should -Match "steamcmd"
        Test-Path -LiteralPath $lockPath | Should -Be $true
    }

    It "lock perime et steamcmd absent : vol de verrou normal (cycle mort confirme)" {
        $lockPath = Join-Path $TestDrive "dead.lock"
        Set-Content -LiteralPath $lockPath -Value "dead"
        (Get-Item -LiteralPath $lockPath).LastWriteTime = (Get-Date).AddMinutes(-15)
        $logPath = Join-Path $TestDrive "locked6.log"
        Mock Test-SteamcmdRunning { $false }

        Invoke-HephAgentCycleLocked -Cfg $script:cfg -Now (Get-Date) -LogPath $logPath -LockPath $lockPath -LockMaxAgeMinutes 10

        Should -Invoke Invoke-HephAgentCycle -Times 1
        Test-Path -LiteralPath $lockPath | Should -Be $false
    }
}

Describe "Test-InAutoUpdateWindow" {
    It "retourne vrai a l'interieur de la fenetre (bornes incluses)" {
        $window = [pscustomobject]@{ start = "05:00"; end = "05:30" }
        Test-InAutoUpdateWindow -Window $window -Now (Get-Date "2026-07-13 05:00") | Should -Be $true
        Test-InAutoUpdateWindow -Window $window -Now (Get-Date "2026-07-13 05:30") | Should -Be $true
        Test-InAutoUpdateWindow -Window $window -Now (Get-Date "2026-07-13 05:15") | Should -Be $true
    }

    It "retourne faux en dehors de la fenetre" {
        $window = [pscustomobject]@{ start = "05:00"; end = "05:30" }
        Test-InAutoUpdateWindow -Window $window -Now (Get-Date "2026-07-13 04:59") | Should -Be $false
        Test-InAutoUpdateWindow -Window $window -Now (Get-Date "2026-07-13 05:31") | Should -Be $false
    }

    It "retourne faux si aucune fenetre configuree" {
        Test-InAutoUpdateWindow -Window $null -Now (Get-Date "2026-07-13 05:15") | Should -Be $false
    }
}
