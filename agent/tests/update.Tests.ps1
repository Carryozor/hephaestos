Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot/../hephaestos-lib.ps1"
}

Describe "Stop-GameServer (palworld-rcon)" {
    BeforeAll {
        # Racine SteamCMD de test : manifest reel (installdir) + ini derive au chemin
        # attendu, pour que Get-ServerInstallDir resolve vers le vrai fichier settings.
        $script:steamRootStop = Join-Path $TestDrive "steam-stop"
        $script:manifestDirStop = Join-Path $script:steamRootStop "steamapps"
        New-Item -ItemType Directory -Path $script:manifestDirStop -Force | Out-Null
        @'
"AppState"
{
	"appid"		"2394010"
	"installdir"		"PalServer"
	"buildid"		"100"
}
'@ | Set-Content -LiteralPath (Join-Path $script:manifestDirStop "appmanifest_2394010.acf") -Encoding UTF8

        $script:iniDirStop = Join-Path $script:steamRootStop "steamapps\common\PalServer\Pal\Saved\Config\WindowsServer"
        New-Item -ItemType Directory -Path $script:iniDirStop -Force | Out-Null
        @'
[/Script/Pal.PalGameWorldSettings]
OptionSettings=(AdminPassword="pwd123")
'@ | Set-Content -LiteralPath (Join-Path $script:iniDirStop "PalWorldSettings.ini") -Encoding UTF8
    }

    BeforeEach {
        $script:cfgStop = [pscustomobject]@{ steamcmd_root = $script:steamRootStop }
        $script:serverCfg = [pscustomobject]@{
            name         = "palworld"
            appid        = 2394010
            process      = "PalServer-Win64-Shipping-Cmd"
            stop_adapter = "palworld-rcon"
            rcon         = [pscustomobject]@{
                host = "127.0.0.1"
                port = 25575
            }
        }

        Mock Invoke-Rcon { return "" }
        Mock Start-Sleep {}
        Mock Get-Process { [pscustomobject]@{ Id = 4242 } }
        Mock Wait-Process {}
        # Depuis le 17/07 l'annonce n'est emise que si des joueurs sont presents
        # (ou comptage inconnu) : ces tests historiques valident le chemin "annonce".
        Mock Get-PalworldPlayers { [pscustomobject]@{ Count = 3; Players = @() } }
    }

    It "diffuse l'avertissement de mise a jour par defaut (pas de -Reason), sauvegarde puis eteint via RCON dans cet ordre exact" {
        Stop-GameServer -Cfg $script:cfgStop -ServerCfg $script:serverCfg

        Should -Invoke Invoke-Rcon -Times 1 -ParameterFilter { $Command -eq "Broadcast Mise_a_jour_dans_60s" }
        Should -Invoke Invoke-Rcon -Times 1 -ParameterFilter { $Command -eq "Save" }
        Should -Invoke Invoke-Rcon -Times 1 -ParameterFilter { $Command -eq "Shutdown 10" }
        Should -Invoke Invoke-Rcon -Times 3
    }

    It "diffuse l'avertissement de redemarrage quand -Reason Redemarrage est fourni" {
        Stop-GameServer -Cfg $script:cfgStop -ServerCfg $script:serverCfg -Reason "Redemarrage"

        Should -Invoke Invoke-Rcon -Times 1 -ParameterFilter { $Command -eq "Broadcast Redemarrage_dans_60s" }
    }

    It "attend 60s apres l'avertissement puis 10s apres la sauvegarde (pas d'autre delai)" {
        Stop-GameServer -Cfg $script:cfgStop -ServerCfg $script:serverCfg

        Should -Invoke Start-Sleep -Times 1 -ParameterFilter { $Seconds -eq 60 }
        Should -Invoke Start-Sleep -Times 1 -ParameterFilter { $Seconds -eq 10 }
        Should -Invoke Start-Sleep -Times 2
    }

    It "attend la fin reelle du process via Wait-Process (pas seulement l'envoi de Shutdown)" {
        Stop-GameServer -Cfg $script:cfgStop -ServerCfg $script:serverCfg

        Should -Invoke Wait-Process -Times 1 -ParameterFilter { $Id -eq 4242 }
    }
}

Describe "Stop-GameServer (generic-graceful)" {
    BeforeEach {
        $script:cfgStop = [pscustomobject]@{ steamcmd_root = "unused" }
        $script:serverCfg = [pscustomobject]@{
            name         = "windrose"
            process      = "WindroseServer"
            stop_adapter = "generic-graceful"
        }

        Mock Get-Process { [pscustomobject]@{ Id = 777 } }
        Mock Invoke-Taskkill {}
    }

    It "se contente du taskkill gracieux (sans /F) si le process se termine dans les 120s" {
        Mock Wait-Process {}

        Stop-GameServer -Cfg $script:cfgStop -ServerCfg $script:serverCfg

        Should -Invoke Invoke-Taskkill -Times 1 -ParameterFilter { ($ArgumentList -join ',') -eq "/PID,777" }
        Should -Invoke Invoke-Taskkill -Times 0 -ParameterFilter { $ArgumentList -contains "/F" }
    }

    It "force le taskkill /F en dernier recours si le process ne se termine pas dans les 120s" {
        Mock Wait-Process { throw "timeout" }

        Stop-GameServer -Cfg $script:cfgStop -ServerCfg $script:serverCfg

        Should -Invoke Invoke-Taskkill -Times 1 -ParameterFilter { ($ArgumentList -join ',') -eq "/PID,777" }
        Should -Invoke Invoke-Taskkill -Times 1 -ParameterFilter { ($ArgumentList -join ',') -eq "/PID,777,/F" }
    }

    It "ne fait rien si le process est deja absent (deja arrete)" {
        Mock Get-Process { $null }
        Mock Wait-Process {}

        Stop-GameServer -Cfg $script:cfgStop -ServerCfg $script:serverCfg

        Should -Invoke Invoke-Taskkill -Times 0
    }

    It "arrete TOUTES les instances si plusieurs process du meme nom tournent simultanement" {
        Mock Get-Process { @(
            [pscustomobject]@{ Id = 111 }
            [pscustomobject]@{ Id = 222 }
        ) }
        Mock Wait-Process {}
        Mock Write-Warning {}

        Stop-GameServer -Cfg $script:cfgStop -ServerCfg $script:serverCfg

        Should -Invoke Invoke-Taskkill -Times 1 -ParameterFilter { ($ArgumentList -join ',') -eq "/PID,111" }
        Should -Invoke Invoke-Taskkill -Times 1 -ParameterFilter { ($ArgumentList -join ',') -eq "/PID,222" }
        Should -Invoke Write-Warning -Times 1
    }
}

Describe "Stop-GameServer (generic-force)" {
    <#
    .NOTES
        Incident 2026-07-14 : le taskkill sans /F echoue IMMEDIATEMENT (erreur Windows
        "ne peut etre arrete que de force") pour le process Windrose -- Stop-GameServer
        attendait quand meme les 120s complets de Wait-Process avant de forcer, a chaque
        stop (verifie en prod : cycle de 16:24:01 a 16:26:02, 121s pour un simple stop).
        "generic-force" saute directement au taskkill /F, sans tentative gracieuse ni
        attente de 120s -- reserve aux jeux dont on sait empiriquement que l'arret gracieux
        ne fonctionne jamais.
    #>
    BeforeEach {
        $script:cfgStop = [pscustomobject]@{ steamcmd_root = "unused" }
        $script:serverCfg = [pscustomobject]@{
            name         = "windrose"
            process      = "WindroseServer"
            stop_adapter = "generic-force"
        }

        Mock Get-Process { [pscustomobject]@{ Id = 777 } }
        Mock Invoke-Taskkill {}
        Mock Wait-Process {}
    }

    It "force le taskkill /F directement, sans tentative gracieuse prealable" {
        Stop-GameServer -Cfg $script:cfgStop -ServerCfg $script:serverCfg

        Should -Invoke Invoke-Taskkill -Times 1
        Should -Invoke Invoke-Taskkill -Times 1 -ParameterFilter { ($ArgumentList -join ',') -eq "/PID,777,/F" }
    }

    It "attend la fin reelle du process via Wait-Process apres le forcage" {
        Stop-GameServer -Cfg $script:cfgStop -ServerCfg $script:serverCfg

        Should -Invoke Wait-Process -Times 1 -ParameterFilter { $Id -eq 777 }
    }

    It "ne fait rien si le process est deja absent (deja arrete)" {
        Mock Get-Process { $null }

        Stop-GameServer -Cfg $script:cfgStop -ServerCfg $script:serverCfg

        Should -Invoke Invoke-Taskkill -Times 0
        Should -Invoke Wait-Process -Times 0
    }

    It "arrete TOUTES les instances si plusieurs process du meme nom tournent simultanement" {
        Mock Get-Process { @(
            [pscustomobject]@{ Id = 111 }
            [pscustomobject]@{ Id = 222 }
        ) }

        Stop-GameServer -Cfg $script:cfgStop -ServerCfg $script:serverCfg

        Should -Invoke Invoke-Taskkill -Times 1 -ParameterFilter { ($ArgumentList -join ',') -eq "/PID,111,/F" }
        Should -Invoke Invoke-Taskkill -Times 1 -ParameterFilter { ($ArgumentList -join ',') -eq "/PID,222,/F" }
    }
}

Describe "Start-GameServer" {
    BeforeEach {
        $script:serverCfg = [pscustomobject]@{
            name       = "palworld"
            process    = "PalServer-Win64-Shipping-Cmd"
            start_task = "PalServer"
        }

        Mock Invoke-Schtasks {}
        Mock Start-Sleep {}
    }

    It "declenche la tache planifiee avec le bon nom puis confirme le process up" {
        Mock Get-Process { [pscustomobject]@{ Id = 999 } }

        Start-GameServer -ServerCfg $script:serverCfg

        Should -Invoke Invoke-Schtasks -Times 1 -ParameterFilter { ($ArgumentList -join ',') -eq "/Run,/TN,PalServer" }
    }

    It "leve une exception explicite si le process n'apparait jamais dans les 60s" {
        Mock Get-Process { $null }

        { Start-GameServer -ServerCfg $script:serverCfg } | Should -Throw "*60s*"
    }

    It "force PriorityClass=High sur le process une fois detecte (schtasks.exe /Run n'honore pas Settings.Priority de la tache -- verifie empiriquement le 19/07/2026)" {
        $fakeProc = [pscustomobject]@{ Id = 999; PriorityClass = "Normal" }
        Mock Get-Process { $fakeProc }

        Start-GameServer -ServerCfg $script:serverCfg

        $fakeProc.PriorityClass | Should -Be "High"
    }

    It "n'echoue pas si l'elevation de PriorityClass leve une exception (best-effort)" {
        $fakeProc = [pscustomobject]@{ Id = 999 }
        $fakeProc | Add-Member -MemberType ScriptProperty -Name PriorityClass `
            -Value { "Normal" } -SecondValue { throw "Acces refuse" }
        Mock Get-Process { $fakeProc }

        { Start-GameServer -ServerCfg $script:serverCfg } | Should -Not -Throw
    }
}

Describe "Update-GameServer" {
    BeforeAll {
        # Stub steamcmd : script PS1 execute comme un vrai binaire (exit isole a son
        # propre appel via l'operateur &, verifie ne tue pas la session Pester parente).
        # Incremente le buildid du manifest situe sous <steamcmd_root>\steamapps\ (racine
        # partagee, bibliotheque PAR DEFAUT de steamcmd -- la racine est fournie au stub
        # via HEPHAESTOS_TEST_STEAMROOT car l'invocation reelle ne doit contenir AUCUN
        # +force_install_dir, cf. incident 14-15/07) puis exit 0, sauf pilotage via
        # variables d'environnement (exit code / pas de bump) pour simuler les cas
        # d'echec steamcmd et de MAJ silencieusement sans effet. Les arguments recus
        # sont journalises dans HEPHAESTOS_TEST_ARGS_LOG pour le test de regression.
        $script:stubPath = Join-Path $TestDrive "steamcmd_stub.ps1"
        @'
param()

$logPath = $env:HEPHAESTOS_TEST_ORDER_LOG
if ($logPath) { Add-Content -LiteralPath $logPath -Value "steamcmd" }

# Regression F1 : un vrai steamcmd emet toujours des centaines de lignes stdout.
# Le stub doit en emettre aussi (succes ET echec) pour que le test puisse detecter
# une sortie non capturee qui polluerait le retour de Update-GameServer.
"Redirecting stderr to 'stub_stderr.log'"
"[  0%] Checking for available update..."
"Success! App '2394010' fully installed."

if ($env:HEPHAESTOS_TEST_ARGS_LOG) { Add-Content -LiteralPath $env:HEPHAESTOS_TEST_ARGS_LOG -Value ($args -join " ") }

$steamRoot = $env:HEPHAESTOS_TEST_STEAMROOT
$appid = $null
for ($i = 0; $i -lt $args.Count; $i++) {
    if ($args[$i] -eq "+app_update") { $appid = $args[$i + 1] }
}

$exitCode = 0
if ($env:HEPHAESTOS_TEST_STEAMCMD_EXIT) { $exitCode = [int]$env:HEPHAESTOS_TEST_STEAMCMD_EXIT }

if ($exitCode -eq 0 -and -not $env:HEPHAESTOS_TEST_STEAMCMD_NO_BUILDID_BUMP) {
    $manifestPath = Join-Path $steamRoot "steamapps\appmanifest_$appid.acf"
    $content = Get-Content -LiteralPath $manifestPath -Raw
    if ($content -match '"buildid"\s+"(\d+)"') {
        $newBuild = [int]$Matches[1] + 1
        $content = $content -replace '"buildid"\s+"\d+"', "`"buildid`"`t`t`"$newBuild`""
        Set-Content -LiteralPath $manifestPath -Value $content -NoNewline
    }
}

exit $exitCode
'@ | Set-Content -LiteralPath $script:stubPath -Encoding UTF8
    }

    BeforeEach {
        $script:steamRoot = Join-Path $TestDrive ([guid]::NewGuid().ToString())
        $script:manifestDir = Join-Path $script:steamRoot "steamapps"
        New-Item -ItemType Directory -Path $script:manifestDir -Force | Out-Null

        $script:manifestPath = Join-Path $script:manifestDir "appmanifest_2394010.acf"
        @'
"AppState"
{
	"appid"		"2394010"
	"buildid"		"100"
}
'@ | Set-Content -LiteralPath $script:manifestPath -Encoding UTF8

        $script:orderLog = Join-Path $TestDrive "order-$([guid]::NewGuid()).log"
        New-Item -ItemType File -Path $script:orderLog -Force | Out-Null
        $env:HEPHAESTOS_TEST_ORDER_LOG = $script:orderLog
        $script:argsLog = Join-Path $TestDrive "args-$([guid]::NewGuid()).log"
        New-Item -ItemType File -Path $script:argsLog -Force | Out-Null
        $env:HEPHAESTOS_TEST_ARGS_LOG = $script:argsLog
        $env:HEPHAESTOS_TEST_STEAMROOT = $script:steamRoot
        Remove-Item Env:\HEPHAESTOS_TEST_STEAMCMD_EXIT -ErrorAction SilentlyContinue
        Remove-Item Env:\HEPHAESTOS_TEST_STEAMCMD_NO_BUILDID_BUMP -ErrorAction SilentlyContinue

        $script:cfg = [pscustomobject]@{ steamcmd = $script:stubPath; steamcmd_root = $script:steamRoot }
        $script:serverCfg = [pscustomobject]@{
            name         = "palworld"
            appid        = 2394010
            process      = "PalServer-Win64-Shipping-Cmd"
            start_task   = "PalServer"
            stop_adapter = "palworld-rcon"
        }

        Mock Stop-GameServer { Add-Content -LiteralPath $script:orderLog -Value "stop" }
        Mock Start-GameServer { Add-Content -LiteralPath $script:orderLog -Value "start" }
    }

    AfterEach {
        Remove-Item Env:\HEPHAESTOS_TEST_ORDER_LOG -ErrorAction SilentlyContinue
        Remove-Item Env:\HEPHAESTOS_TEST_ARGS_LOG -ErrorAction SilentlyContinue
        Remove-Item Env:\HEPHAESTOS_TEST_STEAMROOT -ErrorAction SilentlyContinue
        Remove-Item Env:\HEPHAESTOS_TEST_STEAMCMD_EXIT -ErrorAction SilentlyContinue
        Remove-Item Env:\HEPHAESTOS_TEST_STEAMCMD_NO_BUILDID_BUMP -ErrorAction SilentlyContinue
    }

    It "regression incident 14-15/07 : n'invoque JAMAIS +force_install_dir (la racine steamcmd est deja la bibliotheque par defaut ; le forcer deplace fichiers ou manifest au mauvais endroit)" {
        Update-GameServer -Cfg $script:cfg -ServerCfg $script:serverCfg | Out-Null

        $invokedArgs = (Get-Content -LiteralPath $script:argsLog -Raw)
        $invokedArgs | Should -Match ([regex]::Escape("+app_update 2394010 validate"))
        $invokedArgs | Should -Not -Match ([regex]::Escape("+force_install_dir"))
    }

    It "MAJ reussie : buildid change, ordre stop->steamcmd->start respecte, ok=true" {
        $result = Update-GameServer -Cfg $script:cfg -ServerCfg $script:serverCfg

        $result.ok | Should -Be $true
        Get-LocalBuildId -ManifestPath $script:manifestPath | Should -Be "101"

        Should -Invoke Stop-GameServer -Times 1
        Should -Invoke Start-GameServer -Times 1

        (Get-Content -LiteralPath $script:orderLog -Raw).Trim() -replace "`r`n", "`n" -replace "`n", "," |
            Should -Be "stop,steamcmd,start"
    }

    It "steamcmd exit code != 0 : ok=false ET Start-GameServer appele quand meme (rollback sur l'ancienne version)" {
        $env:HEPHAESTOS_TEST_STEAMCMD_EXIT = "8"

        $result = Update-GameServer -Cfg $script:cfg -ServerCfg $script:serverCfg

        $result.ok | Should -Be $false
        Should -Invoke Start-GameServer -Times 1
        Get-LocalBuildId -ManifestPath $script:manifestPath | Should -Be "100"
    }

    It "buildid inchange apres steamcmd (exit 0 mais MAJ sans effet reel) : ok=false, Start-GameServer quand meme appele" {
        $env:HEPHAESTOS_TEST_STEAMCMD_NO_BUILDID_BUMP = "1"

        $result = Update-GameServer -Cfg $script:cfg -ServerCfg $script:serverCfg

        $result.ok | Should -Be $false
        Should -Invoke Start-GameServer -Times 1
        Get-LocalBuildId -ManifestPath $script:manifestPath | Should -Be "100"
    }

    It "F1 (regression) : la sortie stdout de steamcmd (toujours emise par un vrai steamcmd) ne pollue pas le retour -- reste un pscustomobject{ok,detail} propre, meme en succes" {
        $result = Update-GameServer -Cfg $script:cfg -ServerCfg $script:serverCfg

        $result.GetType().Name | Should -Be "PSCustomObject"
        $result.ok.GetType().Name | Should -Be "Boolean"
        ($result.PSObject.Properties.Name | Sort-Object) -join "," | Should -Be "detail,ok"
    }

    It "F1 (regression) : meme avec sortie stdout, le retour reste propre sur le chemin d'echec (exit != 0)" {
        $env:HEPHAESTOS_TEST_STEAMCMD_EXIT = "8"

        $result = Update-GameServer -Cfg $script:cfg -ServerCfg $script:serverCfg

        $result.GetType().Name | Should -Be "PSCustomObject"
        $result.ok.GetType().Name | Should -Be "Boolean"
        $result.ok | Should -Be $false
        ($result.PSObject.Properties.Name | Sort-Object) -join "," | Should -Be "detail,ok"
    }

    It "F2 : si steamcmd echoue ET le rollback Start-GameServer throw (process qui ne remonte jamais), retourne proprement ok=false avec detail au lieu de laisser l'exception remonter" {
        $env:HEPHAESTOS_TEST_STEAMCMD_EXIT = "8"
        Mock Start-GameServer { throw "Start-GameServer: le process n'est pas monte apres 60s" }

        $script:caughtResult = $null
        { $script:caughtResult = Update-GameServer -Cfg $script:cfg -ServerCfg $script:serverCfg } | Should -Not -Throw

        $script:caughtResult.ok | Should -Be $false
        $script:caughtResult.detail | Should -Match "redemarrage de secours a aussi echoue"
    }

    It "F2 : si le buildid est inchange ET le rollback Start-GameServer throw, retourne proprement ok=false avec detail" {
        $env:HEPHAESTOS_TEST_STEAMCMD_NO_BUILDID_BUMP = "1"
        Mock Start-GameServer { throw "Start-GameServer: le process n'est pas monte apres 60s" }

        $script:caughtResult = $null
        { $script:caughtResult = Update-GameServer -Cfg $script:cfg -ServerCfg $script:serverCfg } | Should -Not -Throw

        $script:caughtResult.ok | Should -Be $false
        $script:caughtResult.detail | Should -Match "redemarrage de secours a aussi echoue"
    }
}

Describe "Restart-GameServer" {
    It "appelle Stop-GameServer puis Start-GameServer, sans jamais toucher a steamcmd" {
        $script:log = Join-Path $TestDrive "restart-order.log"
        New-Item -ItemType File -Path $script:log -Force | Out-Null

        Mock Stop-GameServer { Add-Content -LiteralPath $script:log -Value "stop" }
        Mock Start-GameServer { Add-Content -LiteralPath $script:log -Value "start" }

        $serverCfg = [pscustomobject]@{ name = "windrose" }
        $cfg = [pscustomobject]@{ steamcmd_root = "unused" }
        Restart-GameServer -Cfg $cfg -ServerCfg $serverCfg

        Should -Invoke Stop-GameServer -Times 1
        Should -Invoke Start-GameServer -Times 1
        (Get-Content -LiteralPath $script:log -Raw).Trim() -replace "`r`n", "`n" -replace "`n", "," |
            Should -Be "stop,start"
    }
}
