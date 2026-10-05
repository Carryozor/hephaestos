Set-StrictMode -Version Latest

# Durcissement de l'agent (v2.4.0) : P1 (report de statut borne), P2 (journal
# d'idempotence des ordres), P11 (version), P12 (verrou rafraichi cote cycle).

BeforeAll {
    . "$PSScriptRoot/../hephaestos-lib.ps1"
    . "$PSScriptRoot/../hephaestos-agent.ps1"

    # Racine steam minimale : Send-HephStateReport lit le manifest (comme agent.Tests.ps1).
    $script:jSteamRoot = Join-Path $TestDrive "journal-steam"
    $script:jManifestDir = Join-Path $script:jSteamRoot "steamapps"
    New-Item -ItemType Directory -Path $script:jManifestDir -Force | Out-Null
    @'
"AppState"
{
	"appid"		"2394010"
	"installdir"		"PalServer"
	"buildid"		"100"
}
'@ | Set-Content -LiteralPath (Join-Path $script:jManifestDir "appmanifest_2394010.acf") -Encoding UTF8

    # Exception portant une propriete Response.StatusCode, comme WebException (5.1) /
    # HttpResponseException (7) -- fabriquee a la main, aucun reseau.
    function New-HttpError {
        param([int]$Code, [string]$Message = "Echec appel API Hephaestos Post /api/agent/orders/x")
        $ex = [System.Exception]::new($Message)
        Add-Member -InputObject $ex -NotePropertyName Response -NotePropertyValue ([pscustomobject]@{
            StatusCode = [System.Net.HttpStatusCode]$Code
        })
        return $ex
    }

    function New-JournalCfg {
        $server = [pscustomobject]@{
            name         = "palworld"
            appid        = 2394010
            process      = "PalServer-Win64-Shipping-Cmd"
            start_task   = "PalServer"
            stop_adapter = "palworld-rcon"
            rcon         = [pscustomobject]@{ host = "127.0.0.1"; port = 25575 }
        }
        return [pscustomobject]@{
            api_base           = "http://127.0.0.1:8710"
            agent_token        = "tok"
            kuma_agent_push    = "https://kuma/push/agent"
            steamcmd           = "C:\steam\steamcmd.exe"
            steamcmd_root      = $script:jSteamRoot
            auto_update_window = [pscustomobject]@{ start = "05:00"; end = "05:30" }
            servers            = @($server)
        }
    }

    # Ecrit un journal pre-existant (format identique a celui que produit l'agent).
    function Write-SeedJournal {
        param([string]$Path, [object[]]$Entries)
        @{ entries = @($Entries) } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $Path -Encoding UTF8
    }

    function New-SeedEntry {
        param([string]$Id, [string]$Type, [string]$State, [double]$AgeMinutes = 1, [string]$Detail = $null)
        return @{
            id = $Id; type = $Type; state = $State
            at = (Get-Date).ToUniversalTime().AddMinutes(-$AgeMinutes).ToString("o")
            detail = $Detail
        }
    }
}

Describe "P11 -- version de l'agent" {
    It "annonce 2.4.0 et tient dans les 20 caracteres acceptes par le backend" {
        $script:HephAgentVersion | Should -Be "2.4.0"
        $script:HephAgentVersion.Length | Should -BeLessOrEqual 20
    }
}

Describe "Get-HephHttpStatusCode" {
    It "lit le code sur Exception.Response.StatusCode" {
        $err = try { throw (New-HttpError -Code 409) } catch { $_ }
        Get-HephHttpStatusCode -ErrorRecord $err | Should -Be 409
    }

    It "remonte la chaine d'InnerException (exception enrobee par Invoke-HephApi)" {
        $inner = New-HttpError -Code 503
        $outer = [System.Exception]::new("Echec appel API", $inner)
        $err = try { throw $outer } catch { $_ }
        Get-HephHttpStatusCode -ErrorRecord $err | Should -Be 503
    }

    It "retourne `$null pour une erreur sans reponse HTTP (reseau)" {
        $err = try { throw "connexion refusee" } catch { $_ }
        Get-HephHttpStatusCode -ErrorRecord $err | Should -BeNullOrEmpty
    }

    It "replie sur ErrorDetails quand il porte un status JSON" {
        $rec = [System.Management.Automation.ErrorRecord]::new([System.Exception]::new("x"), "id", "NotSpecified", $null)
        $rec.ErrorDetails = [System.Management.Automation.ErrorDetails]::new('{"status_code": 409, "detail": "deja termine"}')
        $err = try { throw $rec } catch { $_ }
        Get-HephHttpStatusCode -ErrorRecord $err | Should -Be 409
    }

    It "ne prend PAS un '409' perdu dans un message ou un detail pour un code HTTP" {
        $rec = [System.Management.Automation.ErrorRecord]::new([System.Exception]::new("ordre a409b introuvable"), "id", "NotSpecified", $null)
        $rec.ErrorDetails = [System.Management.Automation.ErrorDetails]::new('{"detail": "ordre a409b introuvable"}')
        $err = try { throw $rec } catch { $_ }
        Get-HephHttpStatusCode -ErrorRecord $err | Should -BeNullOrEmpty
    }
}

Describe "Invoke-HephApi -- conserve le code HTTP de l'erreur d'origine" {
    It "le code 409 reste lisible apres l'enrobage de l'exception" {
        Mock Invoke-RestMethod { throw (New-HttpError -Code 409 -Message "conflit") }
        $cfg = [pscustomobject]@{ api_base = "http://x:8710"; agent_token = "t" }
        $err = try { Invoke-HephApi -Cfg $cfg -Method Post -Path "/api/agent/orders/o1" -Body @{ a = 1 } } catch { $_ }
        $err | Should -Not -BeNullOrEmpty
        $err.Exception.Message | Should -Match "Echec appel API Hephaestos Post /api/agent/orders/o1"
        Get-HephHttpStatusCode -ErrorRecord $err | Should -Be 409
    }
}

Describe "Set-HephOrderStatus -- retry borne (P1)" {
    BeforeEach {
        $script:log = Join-Path $TestDrive "status-$(Get-Random).log"
        $script:cfg = New-JournalCfg
        $script:apiCalls = 0
        Mock Start-Sleep {}
    }

    It "succes au premier essai : `$true, un seul appel" {
        Mock Invoke-HephApi { $script:apiCalls++; [pscustomobject]@{ ok = $true } }
        Set-HephOrderStatus -Cfg $script:cfg -OrderId "o1" -Status "done" -LogPath $script:log | Should -BeTrue
        $script:apiCalls | Should -Be 1
    }

    It "erreur transitoire puis succes : retente et retourne `$true" {
        Mock Invoke-HephApi {
            $script:apiCalls++
            if ($script:apiCalls -eq 1) { throw "timeout reseau" }
            [pscustomobject]@{ ok = $true }
        }
        Set-HephOrderStatus -Cfg $script:cfg -OrderId "o1" -Status "done" -LogPath $script:log | Should -BeTrue
        $script:apiCalls | Should -Be 2
    }

    It "echec persistant : exactement 3 tentatives puis `$false (et non `$true) avec une trace dans le log" {
        Mock Invoke-HephApi { $script:apiCalls++; throw "backend indisponible" }
        Set-HephOrderStatus -Cfg $script:cfg -OrderId "o1" -Status "done" -LogPath $script:log | Should -BeFalse
        $script:apiCalls | Should -Be 3
        (Get-Content -LiteralPath $script:log -Raw) | Should -Match "o1.*ERREUR.*done"
    }

    It "backoff injectable : un Start-Sleep entre chaque tentative, avec les delais fournis" {
        Mock Invoke-HephApi { throw "backend indisponible" }
        Set-HephOrderStatus -Cfg $script:cfg -OrderId "o1" -Status "done" -LogPath $script:log -RetryDelaysMs @(7, 9) | Out-Null
        Should -Invoke Start-Sleep -Times 1 -Exactly -ParameterFilter { $Milliseconds -eq 7 }
        Should -Invoke Start-Sleep -Times 1 -Exactly -ParameterFilter { $Milliseconds -eq 9 }
    }

    It "409 detecte par le CODE HTTP : `$false immediat, aucun retry, log 'etat terminal'" {
        Mock Invoke-HephApi { $script:apiCalls++; throw (New-HttpError -Code 409 -Message "Echec") }
        Set-HephOrderStatus -Cfg $script:cfg -OrderId "o1" -Status "running" -LogPath $script:log | Should -BeFalse
        $script:apiCalls | Should -Be 1
        (Get-Content -LiteralPath $script:log -Raw) | Should -Match "etat terminal \(409\)"
    }

    It "uuid d'ordre contenant '409' dans un message d'erreur reseau : PAS pris pour un 409 (3 tentatives)" {
        $uuid = "3f409a2e-0000-4000-8000-409409409409"
        Mock Invoke-HephApi { $script:apiCalls++; throw "Echec appel API Hephaestos Post /api/agent/orders/${uuid}: timeout" }
        Set-HephOrderStatus -Cfg $script:cfg -OrderId $uuid -Status "done" -LogPath $script:log | Should -BeFalse
        $script:apiCalls | Should -Be 3
        (Get-Content -LiteralPath $script:log -Raw) | Should -Not -Match "etat terminal"
    }

    It "409 reel de bout en bout (Invoke-HephApi non mocke, Invoke-RestMethod leve un 409) : un seul appel" {
        Mock Invoke-RestMethod { $script:apiCalls++; throw (New-HttpError -Code 409 -Message "conflit") }
        Set-HephOrderStatus -Cfg $script:cfg -OrderId "o1" -Status "running" -LogPath $script:log | Should -BeFalse
        $script:apiCalls | Should -Be 1
    }

    It "erreur 5xx : transitoire, retentee 3 fois" {
        Mock Invoke-HephApi { $script:apiCalls++; throw (New-HttpError -Code 503 -Message "indisponible") }
        Set-HephOrderStatus -Cfg $script:cfg -OrderId "o1" -Status "done" -LogPath $script:log | Should -BeFalse
        $script:apiCalls | Should -Be 3
    }

    It "erreur 4xx non transitoire (400) : pas de retry, `$false" {
        Mock Invoke-HephApi { $script:apiCalls++; throw (New-HttpError -Code 400 -Message "requete invalide") }
        Set-HephOrderStatus -Cfg $script:cfg -OrderId "o1" -Status "done" -LogPath $script:log | Should -BeFalse
        $script:apiCalls | Should -Be 1
    }
}

Describe "Journal d'idempotence -- primitives (P2)" {
    BeforeEach {
        $script:jp = Join-Path $TestDrive "j-$(Get-Random)\orders-journal.json"
        New-Item -ItemType Directory -Path (Split-Path $script:jp -Parent) -Force | Out-Null
    }

    It "Get-HephJournalPath : orders-journal.json a cote du fichier de config" {
        Get-HephJournalPath -ConfigPath (Join-Path $TestDrive "cfgdir\hephaestos-config.json") |
            Should -Be (Join-Path (Join-Path $TestDrive "cfgdir") "orders-journal.json")
    }

    It "journal absent : repart a vide sans exception" {
        { Read-HephJournal -Path $script:jp } | Should -Not -Throw
        (Read-HephJournal -Path $script:jp).Count | Should -Be 0
    }

    It "journal vide : repart a vide" {
        Set-Content -LiteralPath $script:jp -Value "" -NoNewline
        (Read-HephJournal -Path $script:jp).Count | Should -Be 0
    }

    It "journal corrompu (JSON invalide) : repart a vide sans exception" {
        Set-Content -LiteralPath $script:jp -Value '{"entries": [ {"id": "o1", ' -NoNewline
        { Read-HephJournal -Path $script:jp } | Should -Not -Throw
        (Read-HephJournal -Path $script:jp).Count | Should -Be 0
    }

    It "journal de forme inattendue (tableau, scalaire) : repart a vide" {
        Set-Content -LiteralPath $script:jp -Value '[1,2,3]'
        (Read-HephJournal -Path $script:jp).Count | Should -Be 0
        Set-Content -LiteralPath $script:jp -Value '42'
        (Read-HephJournal -Path $script:jp).Count | Should -Be 0
    }

    It "ecrit started {id,type,state,at} puis done + detail, relisibles" {
        Set-HephJournalEntry -Path $script:jp -Id "o1" -Type "update" -State "started"
        $j = Read-HephJournal -Path $script:jp
        $j["o1"].id | Should -Be "o1"
        $j["o1"].type | Should -Be "update"
        $j["o1"].state | Should -Be "started"
        $j["o1"].at | Should -BeOfType [datetime]
        ([datetime]::UtcNow - $j["o1"].at.ToUniversalTime()).TotalMinutes | Should -BeLessThan 2

        Set-HephJournalEntry -Path $script:jp -Id "o1" -Type "update" -State "done" -Detail "mis a jour"
        $j = Read-HephJournal -Path $script:jp
        $j["o1"].state | Should -Be "done"
        $j["o1"].detail | Should -Be "mis a jour"
        $j.Count | Should -Be 1
    }

    It "champ 'at' en chaine ISO (cas Windows PowerShell 5.1, qui ne convertit pas en [datetime]) : relu en UTC" {
        $raw = [pscustomobject]@{ id = "s1"; type = "update"; state = "started"; at = "2026-10-05T10:30:00.0000000Z" }
        $entry = ConvertTo-HephJournalEntry -Raw $raw
        $entry.at | Should -BeOfType [datetime]
        $entry.at.ToUniversalTime().ToString("yyyy-MM-dd HH:mm") | Should -Be "2026-10-05 10:30"
        (ConvertTo-HephJournalEntry -Raw ([pscustomobject]@{ id = "s2"; type = "update"; state = "started"; at = "pas une date" })) | Should -BeNullOrEmpty
    }

    It "round-trip de Extra (bepinex_installed) sous forme de table" {
        $extra = @{ bepinex_installed = @(@{ slug = "a"; version = "1.0"; paths = @("p1") }) }
        Set-HephJournalEntry -Path $script:jp -Id "o1" -Type "update_bepinex_mods" -State "done" -Detail "ok" -Extra $extra
        $j = Read-HephJournal -Path $script:jp
        $j["o1"].extra | Should -BeOfType [hashtable]
        @($j["o1"].extra.bepinex_installed).Count | Should -Be 1
        @($j["o1"].extra.bepinex_installed)[0].slug | Should -Be "a"
    }

    It "purge les entrees de plus de 48 h a l'ecriture, garde les recentes" {
        Write-SeedJournal -Path $script:jp -Entries @(
            (New-SeedEntry -Id "vieux" -Type "update" -State "done" -AgeMinutes (49 * 60)),
            (New-SeedEntry -Id "recent" -Type "update" -State "done" -AgeMinutes (47 * 60))
        )
        Set-HephJournalEntry -Path $script:jp -Id "neuf" -Type "restart" -State "started"
        $j = Read-HephJournal -Path $script:jp
        $j.ContainsKey("vieux") | Should -BeFalse
        $j.ContainsKey("recent") | Should -BeTrue
        $j.ContainsKey("neuf") | Should -BeTrue
    }

    It "ecriture atomique : ecrit un .tmp puis Move-Item vers la destination, aucun .tmp restant" {
        # Le Move-Item reel ne se re-appelle pas proprement depuis un Mock (@PSBoundParameters) :
        # le deplacement est refait en .NET, l'important etant d'observer l'appel.
        Mock Move-Item { [System.IO.File]::Move($LiteralPath[0], $Destination, $true) }
        Set-HephJournalEntry -Path $script:jp -Id "o1" -Type "update" -State "started"
        Should -Invoke Move-Item -Times 1 -Exactly -ParameterFilter {
            $LiteralPath -like "*.tmp*" -and $Destination -eq $script:jp
        }
        Test-Path -LiteralPath $script:jp | Should -BeTrue
        @(Get-ChildItem -LiteralPath (Split-Path $script:jp -Parent) -Filter "*.tmp*").Count | Should -Be 0
    }

    It "ecriture impossible (dossier parent inexistant et non creable) : ne leve pas" {
        $blocker = Join-Path $TestDrive "un-fichier-$(Get-Random)"
        Set-Content -LiteralPath $blocker -Value "x"
        $badPath = Join-Path $blocker "orders-journal.json"
        { Set-HephJournalEntry -Path $badPath -Id "o1" -Type "update" -State "started" } | Should -Not -Throw
    }

    It "Remove-HephJournalEntry supprime l'id cible uniquement" {
        Set-HephJournalEntry -Path $script:jp -Id "a" -Type "update" -State "started"
        Set-HephJournalEntry -Path $script:jp -Id "b" -Type "update" -State "started"
        Remove-HephJournalEntry -Path $script:jp -Id "a"
        $j = Read-HephJournal -Path $script:jp
        $j.ContainsKey("a") | Should -BeFalse
        $j.ContainsKey("b") | Should -BeTrue
    }
}

Describe "Invoke-HephAgentCycle -- journal d'idempotence (P2)" {
    BeforeEach {
        function Get-OrderPosts {
            param([string]$Id)
            return @($script:calls | Where-Object { $_.Path -eq "/api/agent/orders/${Id}" })
        }

        function Invoke-JCycle {
            $cycleArgs = @{ Cfg = $script:cfg; Now = (Get-Date "2026-10-05 12:00"); LogPath = $script:log; JournalPath = $script:jp }
            Invoke-HephAgentCycle @cycleArgs
        }
        $script:cfg = New-JournalCfg
        $script:log = Join-Path $TestDrive "cycle-$(Get-Random).log"
        $script:jp = Join-Path $TestDrive "cj-$(Get-Random).json"
        $script:calls = @()
        $script:failFinalReport = $false

        Mock Start-Sleep {}
        Mock Get-LocalBuildId { "100" }
        Mock Get-Process { $null }
        Mock Get-PalworldPlayers { [pscustomobject]@{ Count = 0; Players = @() } }
        Mock Get-PublicBuildId { "100" }
        Mock Send-KumaPush {}
        Mock Invoke-HephApi {
            $script:calls += [pscustomobject]@{ Method = $Method; Path = $Path; Body = $Body }
            if ($Path -match '^/api/agent/orders(\?|$)') {
                return [pscustomobject]@{ orders = @($script:pendingOrder) }
            }
            if ($script:failFinalReport -and $Path -like "/api/agent/orders/*" -and $Body.status -ne "running") {
                throw "backend injoignable"
            }
            return [pscustomobject]@{ ok = $true }
        }
    }

    It "ordre neuf : entree 'started' ecrite AVANT l'execution, 'done' + detail apres" {
        $script:pendingOrder = [pscustomobject]@{ id = "j1"; server = "palworld"; type = "update"; status = "pending" }
        $script:stateDuringExec = $null
        Mock Update-GameServer {
            $script:stateDuringExec = (Read-HephJournal -Path $script:jp)["j1"].state
            [pscustomobject]@{ ok = $true; detail = "mise a jour reussie" }
        }

        Invoke-JCycle

        $script:stateDuringExec | Should -Be "started"
        $entry = (Read-HephJournal -Path $script:jp)["j1"]
        $entry.state | Should -Be "done"
        $entry.detail | Should -Be "mise a jour reussie"
        $entry.type | Should -Be "update"
    }

    It "echec d'ordre : entree 'failed' + detail" {
        $script:pendingOrder = [pscustomobject]@{ id = "j2"; server = "palworld"; type = "update"; status = "pending" }
        Mock Update-GameServer { [pscustomobject]@{ ok = $false; detail = "steamcmd a echoue" } }

        Invoke-JCycle

        $entry = (Read-HephJournal -Path $script:jp)["j2"]
        $entry.state | Should -Be "failed"
        $entry.detail | Should -Be "steamcmd a echoue"
    }

    It "id deja TERMINE (done) re-servi par le backend : PAS de re-execution, re-report du statut final stocke (sans repasser par running)" {
        Write-SeedJournal -Path $script:jp -Entries @(New-SeedEntry -Id "j3" -Type "update" -State "done" -AgeMinutes 3 -Detail "mise a jour reussie : 100 -> 101")
        $script:pendingOrder = [pscustomobject]@{ id = "j3"; server = "palworld"; type = "update"; status = "running" }
        Mock Update-GameServer { throw "ne doit pas etre rejoue" }

        Invoke-JCycle

        Should -Invoke Update-GameServer -Times 0 -Exactly
        $posts = Get-OrderPosts -Id "j3"
        $posts.Count | Should -Be 1
        $posts[0].Body.status | Should -Be "done"
        $posts[0].Body.detail | Should -Be "mise a jour reussie : 100 -> 101"
    }

    It "id deja TERMINE (failed) : re-report failed avec le detail stocke" {
        Write-SeedJournal -Path $script:jp -Entries @(New-SeedEntry -Id "j4" -Type "restart" -State "failed" -AgeMinutes 3 -Detail "boom")
        $script:pendingOrder = [pscustomobject]@{ id = "j4"; server = "palworld"; type = "restart"; status = "running" }
        Mock Restart-GameServer { throw "ne doit pas etre rejoue" }

        Invoke-JCycle

        Should -Invoke Restart-GameServer -Times 0 -Exactly
        $posts = Get-OrderPosts -Id "j4"
        $posts[0].Body.status | Should -Be "failed"
        $posts[0].Body.detail | Should -Be "boom"
    }

    It "report final echoue : consigne dans le journal, au cycle suivant re-report SANS re-execution" {
        $script:pendingOrder = [pscustomobject]@{ id = "j5"; server = "palworld"; type = "update"; status = "pending" }
        Mock Update-GameServer { [pscustomobject]@{ ok = $true; detail = "ok-j5" } }
        $script:failFinalReport = $true

        Invoke-JCycle

        Should -Invoke Update-GameServer -Times 1 -Exactly
        $entry = (Read-HephJournal -Path $script:jp)["j5"]
        $entry.state | Should -Be "done"
        $entry.reported | Should -BeFalse

        # Cycle suivant : le backend (ordre toujours 'running') re-sert l'ordre, le backend repond OK.
        $script:failFinalReport = $false
        $script:calls = @()
        Invoke-JCycle

        Should -Invoke Update-GameServer -Times 1 -Exactly
        $posts = Get-OrderPosts -Id "j5"
        $posts.Count | Should -Be 1
        $posts[0].Body.status | Should -Be "done"
        $posts[0].Body.detail | Should -Be "ok-j5"
        (Read-HephJournal -Path $script:jp)["j5"].reported | Should -BeTrue
    }

    It "<type> 'started' sans fin depuis < 15 min : PAS relance, rapporte failed 'interrompu, non rejoue'" -ForEach @(
        @{ type = "restore_save" }, @{ type = "update" }, @{ type = "restart" }, @{ type = "stop" }
    ) {
        Write-SeedJournal -Path $script:jp -Entries @(New-SeedEntry -Id "j6" -Type $type -State "started" -AgeMinutes 5)
        $script:pendingOrder = [pscustomobject]@{ id = "j6"; server = "palworld"; type = $type; status = "running"; backup_file = "x.zip" }
        Mock Update-GameServer { throw "ne doit pas etre rejoue" }
        Mock Restart-GameServer { throw "ne doit pas etre rejoue" }
        Mock Stop-GameServer { throw "ne doit pas etre rejoue" }
        Mock Restore-GameSave { throw "ne doit pas etre rejoue" }

        Invoke-JCycle

        Should -Invoke Update-GameServer -Times 0 -Exactly
        Should -Invoke Restart-GameServer -Times 0 -Exactly
        Should -Invoke Stop-GameServer -Times 0 -Exactly
        Should -Invoke Restore-GameSave -Times 0 -Exactly
        $posts = Get-OrderPosts -Id "j6"
        $posts.Count | Should -Be 1
        $posts[0].Body.status | Should -Be "failed"
        $posts[0].Body.detail | Should -Match "interrompu, non rejoue"
        (Read-HephJournal -Path $script:jp)["j6"].state | Should -Be "failed"
    }

    It "'started' destructif depuis plus de 15 min : executable a nouveau" {
        Write-SeedJournal -Path $script:jp -Entries @(New-SeedEntry -Id "j7" -Type "update" -State "started" -AgeMinutes 20)
        $script:pendingOrder = [pscustomobject]@{ id = "j7"; server = "palworld"; type = "update"; status = "running" }
        Mock Update-GameServer { [pscustomobject]@{ ok = $true; detail = "rejoue" } }

        Invoke-JCycle

        Should -Invoke Update-GameServer -Times 1 -Exactly
        (Read-HephJournal -Path $script:jp)["j7"].state | Should -Be "done"
    }

    It "'started' d'un type NON destructif (backup) depuis < 15 min : re-execute (rejouable sans danger)" {
        Write-SeedJournal -Path $script:jp -Entries @(New-SeedEntry -Id "j8" -Type "backup" -State "started" -AgeMinutes 2)
        $script:pendingOrder = [pscustomobject]@{ id = "j8"; server = "palworld"; type = "backup"; status = "running" }
        Mock Backup-GameSave { "x.zip" }

        Invoke-JCycle

        Should -Invoke Backup-GameSave -Times 1 -Exactly
    }

    Context "journal inscriptible impossible (T2.7)" {
        BeforeEach {
            # Le "dossier" du journal est en fait un fichier : ecriture impossible.
            $blocker = Join-Path $TestDrive "bloque-$(Get-Random)"
            Set-Content -LiteralPath $blocker -Value "x"
            $script:jp = Join-Path $blocker "orders-journal.json"
        }

        It "ordre destructif : NON execute, rapporte failed avec detail explicite, echec loggue" {
            $script:pendingOrder = [pscustomobject]@{ id = "t27a"; server = "palworld"; type = "update"; status = "pending" }
            Mock Update-GameServer { throw "ne doit pas etre execute" }

            Invoke-JCycle

            Should -Invoke Update-GameServer -Times 0 -Exactly
            $failed = @(Get-OrderPosts -Id "t27a" | Where-Object { $_.Body.status -eq "failed" })
            $failed.Count | Should -Be 1
            $failed[0].Body.detail | Should -Match "journal d'idempotence indisponible, ordre destructif non execute"
            @(Get-OrderPosts -Id "t27a" | Where-Object { $_.Body.status -eq "running" }).Count | Should -Be 0
            (Get-Content -LiteralPath $script:log -Raw) | Should -Match "journal.*t27a|t27a.*journal"
        }

        It "ordre non destructif (backup) : execute quand meme, echec du journal loggue" {
            $script:pendingOrder = [pscustomobject]@{ id = "t27b"; server = "palworld"; type = "backup"; status = "pending" }
            Mock Backup-GameSave { "x.zip" }

            Invoke-JCycle

            Should -Invoke Backup-GameSave -Times 1 -Exactly
            (Get-Content -LiteralPath $script:log -Raw) | Should -Match "ecriture du journal impossible"
        }
    }

    It "409 sur le statut running : ordre non execute et entree 'started' retiree (pas de blocage)" {
        $script:pendingOrder = [pscustomobject]@{ id = "j9"; server = "palworld"; type = "update"; status = "pending" }
        Mock Update-GameServer { throw "ne doit pas etre execute" }
        Mock Invoke-HephApi {
            if ($Path -match '^/api/agent/orders(\?|$)') { return [pscustomobject]@{ orders = @($script:pendingOrder) } }
            if ($Path -eq "/api/agent/orders/j9") { throw (New-HttpError -Code 409 -Message "conflit") }
            return [pscustomobject]@{ ok = $true }
        }

        Invoke-JCycle

        Should -Invoke Update-GameServer -Times 0 -Exactly
        (Read-HephJournal -Path $script:jp).ContainsKey("j9") | Should -BeFalse
    }

    It "journal corrompu : le cycle execute l'ordre normalement et reecrit un journal valide" {
        Set-Content -LiteralPath $script:jp -Value '{{{ pas du json'
        $script:pendingOrder = [pscustomobject]@{ id = "j10"; server = "palworld"; type = "update"; status = "pending" }
        Mock Update-GameServer { [pscustomobject]@{ ok = $true; detail = "ok" } }

        { Invoke-JCycle } | Should -Not -Throw

        Should -Invoke Update-GameServer -Times 1 -Exactly
        (Read-HephJournal -Path $script:jp)["j10"].state | Should -Be "done"
    }

    It "ordre de deploiement (install_game) deja termine : pas de re-execution, re-report" {
        Write-SeedJournal -Path $script:jp -Entries @(New-SeedEntry -Id "j11" -Type "install_game" -State "done" -AgeMinutes 4 -Detail "installe")
        $script:pendingOrder = [pscustomobject]@{ id = "j11"; server = "x"; type = "install_game"; appid = 2394010; status = "running" }
        Mock Invoke-InstallGame { throw "ne doit pas etre rejoue" }

        Invoke-JCycle

        Should -Invoke Invoke-InstallGame -Times 0 -Exactly
        $posts = Get-OrderPosts -Id "j11"
        $posts[0].Body.status | Should -Be "done"
        $posts[0].Body.detail | Should -Be "installe"
    }

    It "ordre de deploiement neuf : journalise et exe_candidates conserves pour un re-report" {
        $script:pendingOrder = [pscustomobject]@{ id = "j12"; server = "x"; type = "scan_exe"; appid = 2394010; status = "pending" }
        Mock Invoke-ScanExe { [pscustomobject]@{ ok = $true; detail = "2 exe"; exe_candidates = @("a.exe", "b.exe") } }

        Invoke-JCycle

        $entry = (Read-HephJournal -Path $script:jp)["j12"]
        $entry.state | Should -Be "done"
        @($entry.extra.exe_candidates).Count | Should -Be 2
    }

    It "list_files (lecture seule, contenu potentiellement volumineux/sensible) n'est PAS journalise" {
        $script:pendingOrder = [pscustomobject]@{ id = "j13"; server = "palworld"; type = "list_files"; root = "config"; status = "pending" }
        Mock Invoke-ListFiles { [pscustomobject]@{ ok = $true; detail = "1 fichier"; files = @("a.ini") } }

        Invoke-JCycle

        Should -Invoke Invoke-ListFiles -Times 1 -Exactly
        (Read-HephJournal -Path $script:jp).ContainsKey("j13") | Should -BeFalse
    }

    It "report 'running' impossible (backend injoignable) : ordre non execute, pas d'entree 'started' orpheline" {
        $script:pendingOrder = [pscustomobject]@{ id = "j14"; server = "palworld"; type = "update"; status = "pending" }
        Mock Update-GameServer { throw "ne doit pas etre execute" }
        Mock Invoke-HephApi {
            if ($Path -match '^/api/agent/orders(\?|$)') { return [pscustomobject]@{ orders = @($script:pendingOrder) } }
            if ($Path -eq "/api/agent/orders/j14") { throw "backend injoignable" }
            return [pscustomobject]@{ ok = $true }
        }

        Invoke-JCycle

        Should -Invoke Update-GameServer -Times 0 -Exactly
        (Read-HephJournal -Path $script:jp).ContainsKey("j14") | Should -BeFalse
    }
}

Describe "Invoke-HephAgentCycle -- verrou transmis aux ordres longs (P12)" {
    BeforeEach {
        $script:cfg = New-JournalCfg
        $script:log = Join-Path $TestDrive "lock-$(Get-Random).log"
        $script:jp = Join-Path $TestDrive "lj-$(Get-Random).json"
        $script:lockFile = Join-Path $TestDrive "agent-$(Get-Random).lock"
        Mock Start-Sleep {}
        Mock Get-LocalBuildId { "100" }
        Mock Get-Process { $null }
        Mock Get-PalworldPlayers { [pscustomobject]@{ Count = 0; Players = @() } }
        Mock Get-PublicBuildId { "100" }
        Mock Send-KumaPush {}
        Mock Invoke-HephApi {
            if ($Path -match '^/api/agent/orders(\?|$)') { return [pscustomobject]@{ orders = @($script:pendingOrder) } }
            return [pscustomobject]@{ ok = $true }
        }
    }

    It "ordre stop : Stop-GameServer recoit -LockPath" {
        $script:pendingOrder = [pscustomobject]@{ id = "l1"; server = "palworld"; type = "stop"; status = "pending" }
        Mock Stop-GameServer {}
        Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date) -LogPath $script:log -JournalPath $script:jp -LockPath $script:lockFile
        Should -Invoke Stop-GameServer -Times 1 -Exactly -ParameterFilter { $LockPath -eq $script:lockFile }
    }

    It "ordres update et restart : -LockPath transmis" {
        $script:pendingOrder = [pscustomobject]@{ id = "l2"; server = "palworld"; type = "update"; status = "pending" }
        Mock Update-GameServer { [pscustomobject]@{ ok = $true; detail = "ok" } }
        Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date) -LogPath $script:log -JournalPath $script:jp -LockPath $script:lockFile
        Should -Invoke Update-GameServer -Times 1 -Exactly -ParameterFilter { $LockPath -eq $script:lockFile }

        $script:pendingOrder = [pscustomobject]@{ id = "l3"; server = "palworld"; type = "restart"; status = "pending" }
        Mock Restart-GameServer {}
        Invoke-HephAgentCycle -Cfg $script:cfg -Now (Get-Date) -LogPath $script:log -JournalPath $script:jp -LockPath $script:lockFile
        Should -Invoke Restart-GameServer -Times 1 -Exactly -ParameterFilter { $LockPath -eq $script:lockFile }
    }
}
