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
        Mock Get-Process { [pscustomobject]@{ Id = 4242 } }
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
