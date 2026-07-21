Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot/../hephaestos-lib.ps1"
}

Describe "Get-WindrosePlayers" {
    BeforeAll {
        # Racine SteamCMD de test : manifest reel (installdir), pour que install_dir soit
        # resolu dynamiquement via Get-ServerInstallDir (meme convention que Get-PalworldPlayers).
        $script:steamRootWindrose = Join-Path $TestDrive "steam-windrose"
        $script:manifestDirWindrose = Join-Path $script:steamRootWindrose "steamapps"
        New-Item -ItemType Directory -Path $script:manifestDirWindrose -Force | Out-Null
        @'
"AppState"
{
	"appid"		"4129620"
	"installdir"		"Windrose Dedicated Server"
	"buildid"		"100"
}
'@ | Set-Content -LiteralPath (Join-Path $script:manifestDirWindrose "appmanifest_4129620.acf") -Encoding UTF8

        $script:windrosePlusDir = Join-Path $script:steamRootWindrose "steamapps\common\Windrose Dedicated Server\windrose_plus_data"

        $script:cfgWindrose = [pscustomobject]@{ steamcmd_root = $script:steamRootWindrose }
        $script:serverCfgWindrose = [pscustomobject]@{ appid = 4129620 }
    }

    It "retourne Count `$null et Players vide quand le fichier server_status.json est absent" {
        # Pas de New-Item : le dossier windrose_plus_data n'existe meme pas.
        $result = Get-WindrosePlayers -Cfg $script:cfgWindrose -ServerCfg $script:serverCfgWindrose
        $result.Count | Should -Be $null
        $result.Players.Count | Should -Be 0
    }

    It "retourne Count=3 et 2 joueurs nommes quand le JSON est valide et peuple" {
        New-Item -ItemType Directory -Path $script:windrosePlusDir -Force | Out-Null
        @'
{"timestamp":1784023427,"perf":[],"server":{"version":"0.10.0.6.213","game":"Windrose","windrose_plus":"1.3.15","player_count":3,"invite_code":"d02b0edb","max_players":8,"password_protected":false,"name":"Poorate"},"players":[{"name":"Alice","session_id":"player:1","alive":true},{"name":"Bob","session_id":"player:2","alive":true}]}
'@ | Set-Content -LiteralPath (Join-Path $script:windrosePlusDir "server_status.json") -Encoding UTF8

        $result = Get-WindrosePlayers -Cfg $script:cfgWindrose -ServerCfg $script:serverCfgWindrose
        $result.Count | Should -Be 3
        $result.Players.Count | Should -Be 2
        $result.Players[0].name | Should -Be "Alice"
        $result.Players[0].session_id | Should -Be "player:1"
        $result.Players[0].steamid | Should -Be $null
    }

    It "retourne l'entier 0 (pas `$null) et Players vide quand server.player_count vaut 0" {
        New-Item -ItemType Directory -Path $script:windrosePlusDir -Force | Out-Null
        @'
{"timestamp":1784023427,"perf":[],"server":{"version":"0.10.0.6.213","game":"Windrose","windrose_plus":"1.3.15","player_count":0,"invite_code":"d02b0edb","max_players":8,"password_protected":false,"name":"Poorate"},"players":[]}
'@ | Set-Content -LiteralPath (Join-Path $script:windrosePlusDir "server_status.json") -Encoding UTF8

        $result = Get-WindrosePlayers -Cfg $script:cfgWindrose -ServerCfg $script:serverCfgWindrose
        $result.Count | Should -Be 0
        $result.Count | Should -Not -Be $null
        $result.Players.Count | Should -Be 0
    }

    It "retourne Count `$null quand le fichier contient du JSON malforme (tronque)" {
        New-Item -ItemType Directory -Path $script:windrosePlusDir -Force | Out-Null
        '{"server":{"play' | Set-Content -LiteralPath (Join-Path $script:windrosePlusDir "server_status.json") -Encoding UTF8

        { Get-WindrosePlayers -Cfg $script:cfgWindrose -ServerCfg $script:serverCfgWindrose } | Should -Not -Throw
        (Get-WindrosePlayers -Cfg $script:cfgWindrose -ServerCfg $script:serverCfgWindrose).Count | Should -Be $null
    }

    It "retourne Count `$null quand le JSON est valide mais la cle server est absente" {
        New-Item -ItemType Directory -Path $script:windrosePlusDir -Force | Out-Null
        '{"timestamp":1784023427,"perf":[],"players":[]}' | Set-Content -LiteralPath (Join-Path $script:windrosePlusDir "server_status.json") -Encoding UTF8

        (Get-WindrosePlayers -Cfg $script:cfgWindrose -ServerCfg $script:serverCfgWindrose).Count | Should -Be $null
    }
}
