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

    It "retourne Info/Count `$null (jamais l'objet lui-meme) quand le fichier de log est absent" {
        $result = Get-ValheimLogInfo -LogPath (Join-Path $script:logDir "absent.log")
        $result | Should -Not -Be $null
        $result.Info | Should -Be $null
        $result.Count | Should -Be $null
    }

    It "retourne Info/Count `$null quand le fichier existe mais ne contient aucune ligne de version reconnue" {
        "quelque chose sans rapport`nautre ligne" | Set-Content -LiteralPath $script:logPath -Encoding UTF8
        $result = Get-ValheimLogInfo -LogPath $script:logPath
        $result.Info | Should -Be $null
        $result.Count | Should -Be $null
    }

    It "extrait la version et rapporte Count=0 quand aucun marqueur de jonction n'est present" {
        @'
09/09/2026 17:31:45: Console: Valheim 1.0.7 (network version 39)
09/09/2026 17:31:45: Console:
09/09/2026 17:31:45: Worldgenerator version setup:2
'@ | Set-Content -LiteralPath $script:logPath -Encoding UTF8

        $result = Get-ValheimLogInfo -LogPath $script:logPath
        $result.Info | Should -Match "1\.0\.7"
        $result.Info | Should -Match "network 39"
        $result.Info | Should -Match "0 connexion"
        $result.Count | Should -Be 0
        $result.SteamIds.Count | Should -Be 0
    }

    It "compte les joueurs distincts vus (pas les doublons) via les lignes de jonction reelles et les liste dans SteamIds" {
        # Format REEL observe en prod le 18/09/2026 (1re vraie connexion depuis
        # l'implementation du 09/09/2026) : le nom du personnage suit "from",
        # PAS un SteamID numerique comme la doc communautaire le laissait supposer
        # a l'epoque -- l'ancien regex "from (\d+)" ne matchait donc JAMAIS aucune
        # ligne reelle, Count est reste a 0 en continu pendant 9 jours sans que
        # personne ne le remarque (aucune connexion reelle pour l'exercer avant).
        @'
09/09/2026 17:31:45: Console: Valheim 1.0.7 (network version 39)
09/18/2026 18:25:24: Got character ZDOID from PoukieBear : 3716332063:1
09/18/2026 18:33:17: Got character ZDOID from Pouki : 2539237910:1
09/18/2026 18:40:40: Got character ZDOID from PoukieBear : 0:0
09/18/2026 18:40:40: Got character ZDOID from PoukieBear : 3716332063:2964
'@ | Set-Content -LiteralPath $script:logPath -Encoding UTF8

        $result = Get-ValheimLogInfo -LogPath $script:logPath
        $result.Info | Should -Match "2 connexions"
        $result.Count | Should -Be 2
        (@($result.SteamIds) | Sort-Object) -join "," | Should -Be "Pouki,PoukieBear"
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

        $result = Get-ValheimLogInfo -LogPath $script:logPath
        $result.Info | Should -Match "1\.0\.7"
        $result.Count | Should -Be 0
    }

    It "ne leve jamais d'exception meme sur un fichier vide" {
        "" | Set-Content -LiteralPath $script:logPath -Encoding UTF8
        { Get-ValheimLogInfo -LogPath $script:logPath } | Should -Not -Throw
    }
}
