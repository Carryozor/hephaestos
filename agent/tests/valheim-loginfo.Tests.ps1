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

    It "ne leve jamais d'exception meme sur un fichier vide" {
        "" | Set-Content -LiteralPath $script:logPath -Encoding UTF8
        { Get-ValheimLogInfo -LogPath $script:logPath } | Should -Not -Throw
    }
}
