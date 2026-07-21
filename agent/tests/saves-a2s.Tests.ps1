Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot/../hephaestos-lib.ps1"

    # Fabrique une reponse A2S_INFO valide avec un nombre de joueurs donne.
    function New-A2sInfoResponse {
        param([int]$Players)
        $bytes = [byte[]](0xFF, 0xFF, 0xFF, 0xFF, 0x49, 0x11)
        foreach ($s in @("Mon Serveur", "carte", "valheim", "Valheim")) {
            $bytes += [System.Text.Encoding]::ASCII.GetBytes($s) + [byte[]](0x00)
        }
        $bytes += [byte[]](0x34, 0x0D)      # appid short
        $bytes += [byte[]]($Players, 10)    # players, maxplayers
        return $bytes
    }

    # Racine steam de test partagee : manifest palworld + dossiers de save.
    function New-TestSteamRoot {
        param([string]$Root)
        $manifestDir = Join-Path $Root "steamapps"
        New-Item -ItemType Directory -Path $manifestDir -Force | Out-Null
        @'
"AppState"
{
	"appid"		"2394010"
	"installdir"		"PalServer"
	"buildid"		"100"
}
'@ | Set-Content -LiteralPath (Join-Path $manifestDir "appmanifest_2394010.acf") -Encoding UTF8
        $saveDir = Join-Path $Root "steamapps\common\PalServer\Pal\Saved\SaveGames"
        New-Item -ItemType Directory -Path $saveDir -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $saveDir "Level.sav") -Value "donnees-monde-v1"
        return $saveDir
    }
}

Describe "ConvertFrom-A2sInfoPlayers" {
    It "extrait le nombre de joueurs d'une reponse valide" {
        ConvertFrom-A2sInfoPlayers -Response (New-A2sInfoResponse -Players 7) | Should -Be 7
    }

    It "retourne 0 pour un serveur vide (0 distinct de null)" {
        ConvertFrom-A2sInfoPlayers -Response (New-A2sInfoResponse -Players 0) | Should -Be 0
    }

    It "retourne null pour un paquet challenge (0x41)" {
        $challenge = [byte[]](0xFF, 0xFF, 0xFF, 0xFF, 0x41, 0x01, 0x02, 0x03, 0x04, 0x05)
        ConvertFrom-A2sInfoPlayers -Response $challenge | Should -Be $null
    }

    It "retourne null pour un paquet tronque ou du bruit" {
        ConvertFrom-A2sInfoPlayers -Response ([byte[]](0xFF, 0xFF)) | Should -Be $null
        ConvertFrom-A2sInfoPlayers -Response ([byte[]](0xFF, 0xFF, 0xFF, 0xFF, 0x49, 0x11, 0x41, 0x42)) | Should -Be $null
    }
}

Describe "Get-A2sPlayerCount" {
    It "retourne null quand rien n'ecoute (timeout, pas d'exception)" {
        # port UDP tres probablement libre dans le conteneur de test
        Get-A2sPlayerCount -HostName "127.0.0.1" -Port 48999 -TimeoutMs 300 | Should -Be $null
    }
}

Describe "Backup-GameSave / Get-GameSaveBackups" {
    BeforeEach {
        $script:root = Join-Path $TestDrive "steam-$(Get-Random)"
        $script:saveDir = New-TestSteamRoot -Root $script:root
        $script:cfg = [pscustomobject]@{ steamcmd_root = $script:root }
        $script:srv = [pscustomobject]@{ name = "palworld"; appid = 2394010; save_dir = "Pal\Saved\SaveGames" }
    }

    It "cree un zip nomme <utc>-<kind>.zip et le liste" {
        $file = Backup-GameSave -Cfg $script:cfg -ServerCfg $script:srv -Kind "pre-update"
        $file | Should -Match '^\d{8}-\d{6}-pre-update\.zip$'
        $backups = @(Get-GameSaveBackups -Cfg $script:cfg -ServerCfg $script:srv)
        $backups.Count | Should -Be 1
        $backups[0].file | Should -Be $file
        $backups[0].size_mb | Should -BeGreaterOrEqual 0
        $backups[0].created | Should -Not -BeNullOrEmpty
    }

    It "purge au-dela de backup_keep en gardant les plus recents (tri par nom)" {
        $backupDir = Get-SaveBackupDir -Cfg $script:cfg -ServerCfg $script:srv
        New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
        foreach ($i in 1..12) {
            $stamp = "202607{0:d2}-000000" -f $i
            Set-Content -LiteralPath (Join-Path $backupDir "${stamp}-daily.zip") -Value "x"
        }
        $cfgKeep = [pscustomobject]@{ steamcmd_root = $script:root; backup_keep = 5 }
        Backup-GameSave -Cfg $cfgKeep -ServerCfg $script:srv -Kind "manual" | Out-Null
        $remaining = @(Get-ChildItem -LiteralPath $backupDir -Filter "*.zip")
        $remaining.Count | Should -Be 5
        # les plus anciens (noms 202607 01..08) doivent etre partis
        ($remaining | Where-Object { $_.Name -like "20260701*" }) | Should -BeNullOrEmpty
    }

    It "throw si save_dir absent de la config ou dossier introuvable" {
        $noSave = [pscustomobject]@{ name = "windrose"; appid = 2394010 }
        { Backup-GameSave -Cfg $script:cfg -ServerCfg $noSave } | Should -Throw "*save_dir non configure*"
        $badSave = [pscustomobject]@{ name = "palworld"; appid = 2394010; save_dir = "Pal\Inexistant" }
        { Backup-GameSave -Cfg $script:cfg -ServerCfg $badSave } | Should -Throw "*introuvable*"
    }

    It "liste vide (pas d'exception) quand aucun backup n'existe" {
        @(Get-GameSaveBackups -Cfg $script:cfg -ServerCfg $script:srv).Count | Should -Be 0
    }
}

Describe "Restore-GameSave" {
    BeforeEach {
        $script:root = Join-Path $TestDrive "steam-r-$(Get-Random)"
        $script:saveDir = New-TestSteamRoot -Root $script:root
        $script:cfg = [pscustomobject]@{ steamcmd_root = $script:root }
        $script:srv = [pscustomobject]@{ name = "palworld"; appid = 2394010; save_dir = "Pal\Saved\SaveGames" }
        Mock Stop-GameServer { }
        Mock Start-GameServer { }
    }

    It "restaure le contenu du zip et prend une copie de surete avant" {
        $backup = Backup-GameSave -Cfg $script:cfg -ServerCfg $script:srv -Kind "manual"
        # la save actuelle evolue (et se corrompt, disons) apres le backup
        Set-Content -LiteralPath (Join-Path $script:saveDir "Level.sav") -Value "donnees-corrompues"

        $detail = Restore-GameSave -Cfg $script:cfg -ServerCfg $script:srv -BackupFile $backup

        Get-Content -LiteralPath (Join-Path $script:saveDir "Level.sav") | Should -Be "donnees-monde-v1"
        $detail | Should -Match "restauration de .* effectuee"
        $detail | Should -Match "pre-restore"
        Should -Invoke Stop-GameServer -Times 1 -Exactly
        Should -Invoke Start-GameServer -Times 1 -Exactly
        # la copie de surete contient bien la version "corrompue" remplacee
        $preRestore = @(Get-GameSaveBackups -Cfg $script:cfg -ServerCfg $script:srv) |
            Where-Object { $_.file -like "*pre-restore*" }
        $preRestore | Should -Not -BeNullOrEmpty
    }

    It "refuse un nom de backup avec traversal ou format invalide" {
        foreach ($bad in @("..\..\evil.zip", "a/b.zip", "x.txt", "$([char]0)x.zip")) {
            { Restore-GameSave -Cfg $script:cfg -ServerCfg $script:srv -BackupFile $bad } |
                Should -Throw "*invalide*"
        }
        Should -Invoke Stop-GameServer -Times 0 -Exactly
    }

    It "throw si le backup n'existe pas (avant tout arret du serveur)" {
        { Restore-GameSave -Cfg $script:cfg -ServerCfg $script:srv -BackupFile "20990101-000000-daily.zip" } |
            Should -Throw "*introuvable*"
        Should -Invoke Stop-GameServer -Times 0 -Exactly
    }
}

Describe "Stop-GameServer (palworld-rcon) : annonce seulement si joueurs" {
    BeforeEach {
        $script:root = Join-Path $TestDrive "steam-w-$(Get-Random)"
        New-TestSteamRoot -Root $script:root | Out-Null
        $iniDir = Join-Path $script:root "steamapps\common\PalServer\Pal\Saved\Config\WindowsServer"
        New-Item -ItemType Directory -Path $iniDir -Force | Out-Null
        @'
[/Script/Pal.PalGameWorldSettings]
OptionSettings=(AdminPassword="pwd123")
'@ | Set-Content -LiteralPath (Join-Path $iniDir "PalWorldSettings.ini") -Encoding UTF8

        $script:cfg = [pscustomobject]@{ steamcmd_root = $script:root }
        $script:srv = [pscustomobject]@{
            name = "palworld"; appid = 2394010; process = "hephaestos-test-inexistant"
            stop_adapter = "palworld-rcon"
            rcon = [pscustomobject]@{ host = "127.0.0.1"; port = 25575 }
        }
        Mock Invoke-Rcon { "" }
        Mock Start-Sleep { }
        # Depuis le fix du 18/07, Stop-GameServer verifie la presence du process en
        # entree et retourne sans rien faire s'il est absent : ces tests valident la
        # politique d'annonce, donc on simule un process vivant (et on neutralise le
        # Wait-Process final qui, sans mock, attendrait un vrai PID 999 jusqu'a 120s).
        Mock Get-Process { [pscustomobject]@{ Id = 999 } }
        Mock Wait-Process { }
    }

    It "serveur prouve vide (0) : pas de Broadcast, pas d'attente longue" {
        Mock Get-PalworldPlayers { [pscustomobject]@{ Count = 0; Players = @() } }
        Stop-GameServer -Cfg $script:cfg -ServerCfg $script:srv -Reason "Test"
        Should -Invoke Invoke-Rcon -Times 0 -Exactly -ParameterFilter { $Command -like "Broadcast*" }
        Should -Invoke Start-Sleep -Times 0 -Exactly -ParameterFilter { $Seconds -ge 60 }
        Should -Invoke Invoke-Rcon -Times 1 -Exactly -ParameterFilter { $Command -eq "Save" }
    }

    It "joueurs presents : Broadcast avec le delai configure puis attente" {
        Mock Get-PalworldPlayers { [pscustomobject]@{ Count = 2; Players = @() } }
        $script:srv | Add-Member -NotePropertyName stop_warn_seconds -NotePropertyValue 30
        Stop-GameServer -Cfg $script:cfg -ServerCfg $script:srv -Reason "Redemarrage"
        Should -Invoke Invoke-Rcon -Times 1 -Exactly -ParameterFilter { $Command -eq "Broadcast Redemarrage_dans_30s" }
        Should -Invoke Start-Sleep -Times 1 -Exactly -ParameterFilter { $Seconds -eq 30 }
    }

    It "comptage inconnu (null) : prudence, on annonce comme avant" {
        Mock Get-PalworldPlayers { throw "rcon KO" }
        Stop-GameServer -Cfg $script:cfg -ServerCfg $script:srv -Reason "Test"
        Should -Invoke Invoke-Rcon -Times 1 -Exactly -ParameterFilter { $Command -like "Broadcast Test_dans_60s" }
        Should -Invoke Start-Sleep -Times 1 -Exactly -ParameterFilter { $Seconds -eq 60 }
    }
}
