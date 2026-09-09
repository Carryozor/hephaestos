Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot/../hephaestos-lib.ps1"
}

Describe "Get-ValheimLogInfo" {
    BeforeAll {
        $script:logDir = Join-Path $TestDrive "valheim-log"
        New-Item -ItemType Directory -Path $script:logDir -Force | Out-Null
        $script:logPath = Join-Path $script:logDir "valheim-console.log"
    }

    It "retourne `$null quand le fichier de log est absent" {
        Get-ValheimLogInfo -LogPath (Join-Path $script:logDir "absent.log") | Should -Be $null
    }

    It "retourne `$null quand le fichier existe mais ne contient aucune ligne de version reconnue" {
        "quelque chose sans rapport`nautre ligne" | Set-Content -LiteralPath $script:logPath -Encoding UTF8
        Get-ValheimLogInfo -LogPath $script:logPath | Should -Be $null
    }

    It "extrait la version et rapporte 0 connexion vue quand aucun marqueur de jonction n'est present" {
        @'
09/09/2026 17:31:45: Console: Valheim 1.0.7 (network version 39)
09/09/2026 17:31:45: Console:
09/09/2026 17:31:45: Worldgenerator version setup:2
'@ | Set-Content -LiteralPath $script:logPath -Encoding UTF8

        $info = Get-ValheimLogInfo -LogPath $script:logPath
        $info | Should -Match "1\.0\.7"
        $info | Should -Match "network 39"
        $info | Should -Match "0 connexion"
    }

    It "compte les SteamID distincts vus (pas les doublons) via les lignes de jonction connues" {
        @'
09/09/2026 17:31:45: Console: Valheim 1.0.7 (network version 39)
09/09/2026 17:35:02: Got character ZDOID from 76561198012345678 : 123:456
09/09/2026 17:35:10: Got character ZDOID from 76561198012345678 : 123:457
09/09/2026 17:40:00: Got character ZDOID from 76561198098765432 : 200:1
'@ | Set-Content -LiteralPath $script:logPath -Encoding UTF8

        $info = Get-ValheimLogInfo -LogPath $script:logPath
        $info | Should -Match "2 connexions"
    }

    It "trouve la ligne de version meme apres des milliers de lignes suivantes (serveur up depuis longtemps)" {
        # Regression du bug reel du 09/09/2026 : la ligne de version n'apparait qu'UNE
        # FOIS au tout debut du log (au boot) -- un ancien -Tail 500 la faisait sortir de
        # la fenetre des qu'assez de lignes periodiques ("Connections 0 ZDOS...", toutes
        # les quelques minutes) s'accumulaient, et rcon_info restait $null indefiniment
        # malgre un log valide.
        $header = "09/09/2026 17:31:45: Console: Valheim 1.0.7 (network version 39)"
        $filler = 1..800 | ForEach-Object { "09/09/2026 20:00:$($_ % 60): Connections 0 ZDOS:15577  sent:0 recv:0" }
        @($header) + $filler | Set-Content -LiteralPath $script:logPath -Encoding UTF8

        $info = Get-ValheimLogInfo -LogPath $script:logPath
        $info | Should -Match "1\.0\.7"
    }

    It "ne leve jamais d'exception meme sur un fichier vide" {
        "" | Set-Content -LiteralPath $script:logPath -Encoding UTF8
        { Get-ValheimLogInfo -LogPath $script:logPath } | Should -Not -Throw
    }
}
