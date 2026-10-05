Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot/../hephaestos-lib.ps1"
}

Describe "Stop-GameServer (palworld-rcon) - resilience serveur mort/mourant" {
    BeforeAll {
        # Meme fixture que update.Tests.ps1 : manifest .acf + ini derive, pour que
        # Get-ServerInstallDir / Get-PalworldAdminPassword resolvent normalement.
        $script:steamRootRes = Join-Path $TestDrive "steam-stop-res"
        $script:manifestDirRes = Join-Path $script:steamRootRes "steamapps"
        New-Item -ItemType Directory -Path $script:manifestDirRes -Force | Out-Null
        @'
"AppState"
{
	"appid"		"2394010"
	"installdir"		"PalServer"
	"buildid"		"100"
}
'@ | Set-Content -LiteralPath (Join-Path $script:manifestDirRes "appmanifest_2394010.acf") -Encoding UTF8

        $script:iniDirRes = Join-Path $script:steamRootRes "steamapps\common\PalServer\Pal\Saved\Config\WindowsServer"
        New-Item -ItemType Directory -Path $script:iniDirRes -Force | Out-Null
        @'
[/Script/Pal.PalGameWorldSettings]
OptionSettings=(AdminPassword="pwd123")
'@ | Set-Content -LiteralPath (Join-Path $script:iniDirRes "PalWorldSettings.ini") -Encoding UTF8
    }

    BeforeEach {
        $script:cfgRes = [pscustomobject]@{ steamcmd_root = $script:steamRootRes }
        $script:serverCfgRes = [pscustomobject]@{
            name         = "palworld"
            appid        = 2394010
            process      = "PalServer-Win64-Shipping-Cmd"
            stop_adapter = "palworld-rcon"
            rcon         = [pscustomobject]@{
                host = "127.0.0.1"
                port = 25575
            }
        }
        Mock Invoke-Taskkill {}
        Mock Wait-Process {}
        Mock Start-Sleep {}
    }

    It "serveur deja arrete a l'entree : aucun appel RCON, aucun taskkill, retour sans exception" {
        Mock Get-Process { $null }
        Mock Invoke-Rcon { throw "ne doit pas etre appele" }

        { Stop-GameServer -Cfg $script:cfgRes -ServerCfg $script:serverCfgRes } | Should -Not -Throw

        Should -Invoke Invoke-Rcon -Times 0 -Exactly
        Should -Invoke Invoke-Taskkill -Times 0 -Exactly
    }

    It "RCON refuse en pleine sequence avec process encore vivant : fallback taskkill /F, pas d'exception" {
        # Le process meurt une fois le taskkill /F emis (sinon la verification finale P6
        # le declarerait a juste titre toujours vivant).
        $script:killed = $false
        Mock Wait-Process { $script:killed = $true }
        Mock Get-Process { if ($script:killed) { $null } else { [pscustomobject]@{ Id = 4242 } } }
        Mock Invoke-Rcon { throw "No connection could be made" }
        Mock Get-PalworldPlayers { [pscustomobject]@{ Count = 0; Players = @() } }

        { Stop-GameServer -Cfg $script:cfgRes -ServerCfg $script:serverCfgRes } | Should -Not -Throw

        Should -Invoke Invoke-Taskkill -Times 1 -ParameterFilter {
            $ArgumentList -contains "/F" -and $ArgumentList -contains "4242"
        }
        Should -Invoke Wait-Process -Times 1
    }

    It "process mort au moment du fallback : pas de taskkill" {
        $script:getProcessCallCount = 0
        Mock Get-Process {
            $script:getProcessCallCount++
            if ($script:getProcessCallCount -eq 1) {
                return [pscustomobject]@{ Id = 4242 }
            }
            return $null
        }
        Mock Invoke-Rcon { throw "No connection could be made" }
        Mock Get-PalworldPlayers { [pscustomobject]@{ Count = 0; Players = @() } }

        { Stop-GameServer -Cfg $script:cfgRes -ServerCfg $script:serverCfgRes } | Should -Not -Throw

        Should -Invoke Invoke-Taskkill -Times 0 -Exactly
    }
}

Describe "Stop-GameServer (palworld-rcon) - process rebelle apres Shutdown (P6)" {
    BeforeAll {
        $script:steamRootP6 = Join-Path $TestDrive "steam-stop-p6"
        $manifestDir = Join-Path $script:steamRootP6 "steamapps"
        New-Item -ItemType Directory -Path $manifestDir -Force | Out-Null
        @'
"AppState"
{
	"appid"		"2394010"
	"installdir"		"PalServer"
	"buildid"		"100"
}
'@ | Set-Content -LiteralPath (Join-Path $manifestDir "appmanifest_2394010.acf") -Encoding UTF8
        $iniDir = Join-Path $script:steamRootP6 "steamapps\common\PalServer\Pal\Saved\Config\WindowsServer"
        New-Item -ItemType Directory -Path $iniDir -Force | Out-Null
        "OptionSettings=(AdminPassword=`"pwd123`")" | Set-Content -LiteralPath (Join-Path $iniDir "PalWorldSettings.ini") -Encoding UTF8
    }

    BeforeEach {
        $script:cfgP6 = [pscustomobject]@{ steamcmd_root = $script:steamRootP6 }
        $script:srvP6 = [pscustomobject]@{
            name = "palworld"; appid = 2394010; process = "PalServer-Win64-Shipping-Cmd"
            stop_adapter = "palworld-rcon"
            rcon = [pscustomobject]@{ host = "127.0.0.1"; port = 25575 }
        }
        $script:killedP6 = $false
        Mock Start-Sleep {}
        Mock Wait-Process {}
        Mock Get-PalworldPlayers { [pscustomobject]@{ Count = 0; Players = @() } }
        Mock Invoke-Rcon {}
    }

    It "process encore vivant apres Wait-Process 120 s : taskkill /F puis nouvelle attente courte, pas d'exception s'il meurt" {
        Mock Invoke-Taskkill { $script:killedP6 = $true }
        Mock Get-Process { if ($script:killedP6) { $null } else { [pscustomobject]@{ Id = 4242 } } }

        { Stop-GameServer -Cfg $script:cfgP6 -ServerCfg $script:srvP6 } | Should -Not -Throw

        Should -Invoke Invoke-Taskkill -Times 1 -Exactly -ParameterFilter {
            $ArgumentList -contains "/F" -and $ArgumentList -contains "4242"
        }
        Should -Invoke Wait-Process -Times 1 -Exactly -ParameterFilter { $Timeout -eq 120 }
        Should -Invoke Wait-Process -Times 1 -Exactly -ParameterFilter { $Timeout -lt 120 -and $Timeout -gt 0 }
    }

    It "process toujours vivant apres le taskkill /F : throw (l'ordre ne doit pas se croire 'arrete')" {
        Mock Invoke-Taskkill {}
        Mock Get-Process { [pscustomobject]@{ Id = 4242 } }

        { Stop-GameServer -Cfg $script:cfgP6 -ServerCfg $script:srvP6 } | Should -Throw "*toujours en vie*"

        Should -Invoke Invoke-Taskkill -Times 1 -Exactly -ParameterFilter { $ArgumentList -contains "/F" }
    }

    It "process mort a temps (Shutdown efficace) : aucun taskkill, une seule attente" {
        $script:downP6 = $false
        Mock Invoke-Rcon { if ($Command -like "Shutdown*") { $script:downP6 = $true } }
        Mock Get-Process { if ($script:downP6) { $null } else { [pscustomobject]@{ Id = 4242 } } }
        Mock Invoke-Taskkill {}

        { Stop-GameServer -Cfg $script:cfgP6 -ServerCfg $script:srvP6 } | Should -Not -Throw

        Should -Invoke Invoke-Taskkill -Times 0 -Exactly
    }
}

Describe "Restart-GameServer sur serveur deja mort" {
    It "atteint quand meme Start-GameServer" {
        Mock Get-Process { $null }
        Mock Start-GameServer {}
        Mock Get-GameSaveDir { $null }

        $cfg = [pscustomobject]@{ steamcmd_root = (Join-Path $TestDrive "steam-restart-dead") }
        $serverCfg = [pscustomobject]@{
            name         = "palworld"
            appid        = 2394010
            process      = "PalServer-Win64-Shipping-Cmd"
            stop_adapter = "palworld-rcon"
            rcon         = [pscustomobject]@{
                host = "127.0.0.1"
                port = 25575
            }
        }

        Restart-GameServer -Cfg $cfg -ServerCfg $serverCfg

        Should -Invoke Start-GameServer -Times 1 -Exactly
    }
}
