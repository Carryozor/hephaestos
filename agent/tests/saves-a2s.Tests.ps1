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
        # P7 : la retention est appliquee PAR TYPE -- 5 daily gardes + le nouveau manual.
        $remaining.Count | Should -Be 6
        @($remaining | Where-Object { $_.Name -like "*-daily.zip" }).Count | Should -Be 5
        # les plus anciens daily (noms 202607 01..07) doivent etre partis
        ($remaining | Where-Object { $_.Name -like "20260701*" }) | Should -BeNullOrEmpty
        ($remaining | Where-Object { $_.Name -like "20260712*" }) | Should -Not -BeNullOrEmpty
    }

    It "P7 : une rafale de pre-* n'evince plus les sauvegardes quotidiennes" {
        $backupDir = Get-SaveBackupDir -Cfg $script:cfg -ServerCfg $script:srv
        New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
        foreach ($i in 1..3) {
            Set-Content -LiteralPath (Join-Path $backupDir ("202607{0:d2}-040000-daily.zip" -f $i)) -Value "d"
        }
        foreach ($i in 10..24) {
            Set-Content -LiteralPath (Join-Path $backupDir ("202607{0:d2}-120000-pre-update.zip" -f $i)) -Value "p"
        }
        $cfgKeep = [pscustomobject]@{ steamcmd_root = $script:root; backup_keep = 4 }
        Backup-GameSave -Cfg $cfgKeep -ServerCfg $script:srv -Kind "pre-update" | Out-Null

        $names = @(Get-ChildItem -LiteralPath $backupDir -Filter "*.zip" | ForEach-Object { $_.Name })
        @($names | Where-Object { $_ -like "*-daily.zip" }).Count | Should -Be 3
        @($names | Where-Object { $_ -like "*-pre-update.zip" }).Count | Should -Be 4
        # et ce sont bien les pre-update les plus RECENTS qui restent
        ($names | Where-Object { $_ -like "20260710-*" }) | Should -BeNullOrEmpty
    }

    It "P7 : retention par type, noms hors schema regroupes ensemble (jamais supprimes par erreur sous le quota)" {
        $backupDir = Get-SaveBackupDir -Cfg $script:cfg -ServerCfg $script:srv
        New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $backupDir "manuel-ancien.zip") -Value "x"
        $cfgKeep = [pscustomobject]@{ steamcmd_root = $script:root; backup_keep = 2 }
        Backup-GameSave -Cfg $cfgKeep -ServerCfg $script:srv -Kind "manual" | Out-Null
        Test-Path -LiteralPath (Join-Path $backupDir "manuel-ancien.zip") | Should -BeTrue
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
        # v2.4.0 : le process doit disparaitre apres l'attente, sinon taskkill /F + exception.
        $script:procGone = $false
        Mock Get-Process { if ($script:procGone) { $null } else { [pscustomobject]@{ Id = 999 } } }
        Mock Wait-Process { $script:procGone = $true }
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


Describe "Restore-GameSave -- echec de relance consigne (T2.6)" {
    BeforeEach {
        $script:root = Join-Path $TestDrive "steam-t26-$(Get-Random)"
        $script:saveDir = New-TestSteamRoot -Root $script:root
        $script:cfg = [pscustomobject]@{ steamcmd_root = $script:root }
        $script:srv = [pscustomobject]@{ name = "palworld"; appid = 2394010; save_dir = "Pal\Saved\SaveGames" }
        Mock Stop-GameServer { }
    }

    It "restauration en echec ET relance en echec : le message final contient les deux causes, Write-Warning emis" {
        $backup = Backup-GameSave -Cfg $script:cfg -ServerCfg $script:srv -Kind "manual"
        Mock Backup-GameSave { throw "disque plein" }
        Mock Start-GameServer { throw "tache planifiee introuvable" }
        Mock Write-Warning { }

        $err = $null
        try { Restore-GameSave -Cfg $script:cfg -ServerCfg $script:srv -BackupFile $backup } catch { $err = $_.Exception.Message }

        $err | Should -Match "disque plein"
        $err | Should -Match "ATTENTION le serveur n'a pas pu etre relance: tache planifiee introuvable"
        Should -Invoke Write-Warning -Times 1 -ParameterFilter { $Message -match "tache planifiee introuvable" }
    }

    It "restauration en echec, relance reussie : message d'origine inchange (pas de mention ATTENTION)" {
        $backup = Backup-GameSave -Cfg $script:cfg -ServerCfg $script:srv -Kind "manual"
        Mock Backup-GameSave { throw "disque plein" }
        Mock Start-GameServer { }

        $err = $null
        try { Restore-GameSave -Cfg $script:cfg -ServerCfg $script:srv -BackupFile $backup } catch { $err = $_.Exception.Message }

        $err | Should -Match "disque plein"
        $err | Should -Not -Match "ATTENTION"
    }
}

Describe "Restore-GameSave -- durcissement (P5)" {
    BeforeEach {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $script:root = Join-Path $TestDrive "steam-p5-$(Get-Random)"
        $script:saveDir = New-TestSteamRoot -Root $script:root
        $script:cfg = [pscustomobject]@{ steamcmd_root = $script:root }
        $script:srv = [pscustomobject]@{ name = "palworld"; appid = 2394010; save_dir = "Pal\Saved\SaveGames" }
        $script:backupDir = Get-SaveBackupDir -Cfg $script:cfg -ServerCfg $script:srv
        Mock Stop-GameServer { }
        Mock Start-GameServer { }

        # Fabrique un zip aux entrees arbitraires (noms hostiles inclus) directement dans le dossier de backups.
        function New-CustomZip {
            param([string]$Name, [string[]]$EntryNames)
            New-Item -ItemType Directory -Path $script:backupDir -Force | Out-Null
            $zipPath = Join-Path $script:backupDir $Name
            $zip = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
            try {
                foreach ($entryName in $EntryNames) {
                    $entry = $zip.CreateEntry($entryName)
                    $writer = [System.IO.StreamWriter]::new($entry.Open())
                    $writer.Write("contenu")
                    $writer.Dispose()
                }
            } finally { $zip.Dispose() }
            return $zipPath
        }

        function Get-SiblingNames {
            return @(Get-ChildItem -LiteralPath (Split-Path $script:saveDir -Parent) | ForEach-Object { $_.Name })
        }
    }

    It "zip tronque : refuse AVANT toute ecriture, save d'origine intacte, serveur non arrete" {
        $backup = Backup-GameSave -Cfg $script:cfg -ServerCfg $script:srv -Kind "manual"
        $zipPath = Join-Path $script:backupDir $backup
        $bytes = [System.IO.File]::ReadAllBytes($zipPath)
        $half = [byte[]]::new([int]($bytes.Length / 2))
        [Array]::Copy($bytes, $half, $half.Length)
        [System.IO.File]::WriteAllBytes($zipPath, $half)

        { Restore-GameSave -Cfg $script:cfg -ServerCfg $script:srv -BackupFile $backup } | Should -Throw

        Get-Content -LiteralPath (Join-Path $script:saveDir "Level.sav") | Should -Be "donnees-monde-v1"
        Should -Invoke Stop-GameServer -Times 0 -Exactly
        (Get-SiblingNames) | Should -Be @("SaveGames")
    }

    It "zip dont une entree contient '..' : refuse, save intacte, serveur non arrete (<entry>)" -ForEach @(
        @{ entry = "..\evil.txt" },
        @{ entry = "ok/../../evil.txt" },
        @{ entry = "a/b/../../../evil.txt" }
    ) {
        $zipPath = New-CustomZip -Name "20990101-000000-hostile.zip" -EntryNames @("Level.sav", $entry)
        { Restore-GameSave -Cfg $script:cfg -ServerCfg $script:srv -BackupFile "20990101-000000-hostile.zip" } |
            Should -Throw "*chemin*"
        Get-Content -LiteralPath (Join-Path $script:saveDir "Level.sav") | Should -Be "donnees-monde-v1"
        Should -Invoke Stop-GameServer -Times 0 -Exactly
    }

    It "zip avec entree a chemin absolu (<entry>) : refuse" -ForEach @(
        @{ entry = "/etc/cron.d/x" },
        @{ entry = "\Windows\x.dll" },
        @{ entry = "C:\Windows\x.dll" }
    ) {
        New-CustomZip -Name "20990101-000000-abs.zip" -EntryNames @($entry) | Out-Null
        { Restore-GameSave -Cfg $script:cfg -ServerCfg $script:srv -BackupFile "20990101-000000-abs.zip" } |
            Should -Throw "*chemin*"
        Get-Content -LiteralPath (Join-Path $script:saveDir "Level.sav") | Should -Be "donnees-monde-v1"
    }

    It "echec de la copie de surete alors que la save existe : ABANDON, aucune suppression, serveur relance" {
        $backup = Backup-GameSave -Cfg $script:cfg -ServerCfg $script:srv -Kind "manual"
        Set-Content -LiteralPath (Join-Path $script:saveDir "Level.sav") -Value "donnees-actuelles"
        Mock Backup-GameSave { throw "disque plein" }

        { Restore-GameSave -Cfg $script:cfg -ServerCfg $script:srv -BackupFile $backup } | Should -Throw "*copie de surete*"

        Get-Content -LiteralPath (Join-Path $script:saveDir "Level.sav") | Should -Be "donnees-actuelles"
        (Get-SiblingNames) | Should -Be @("SaveGames")
        Should -Invoke Start-GameServer -Times 1 -Exactly
    }

    It "extraction qui echoue : save d'origine intacte et dossier temporaire nettoye" {
        $backup = Backup-GameSave -Cfg $script:cfg -ServerCfg $script:srv -Kind "manual"
        Set-Content -LiteralPath (Join-Path $script:saveDir "Level.sav") -Value "donnees-actuelles"
        Mock Expand-Archive { throw "archive corrompue en cours d'extraction" }

        { Restore-GameSave -Cfg $script:cfg -ServerCfg $script:srv -BackupFile $backup } | Should -Throw "*extraction*"

        Get-Content -LiteralPath (Join-Path $script:saveDir "Level.sav") | Should -Be "donnees-actuelles"
        (Get-SiblingNames) | Should -Be @("SaveGames")
    }

    It "echec du deplacement de la nouvelle save en place : l'ancienne save est restauree" {
        $backup = Backup-GameSave -Cfg $script:cfg -ServerCfg $script:srv -Kind "manual"
        Set-Content -LiteralPath (Join-Path $script:saveDir "Level.sav") -Value "donnees-actuelles"
        $script:failedOnce = $false
        $target = $script:saveDir
        Mock Move-Item {
            if (-not $script:failedOnce -and $Destination -eq $target) {
                $script:failedOnce = $true
                throw "acces refuse"
            }
            [System.IO.Directory]::Move($LiteralPath[0], $Destination)
        }

        { Restore-GameSave -Cfg $script:cfg -ServerCfg $script:srv -BackupFile $backup } | Should -Throw

        Get-Content -LiteralPath (Join-Path $script:saveDir "Level.sav") | Should -Be "donnees-actuelles"
        (Get-SiblingNames) | Should -Be @("SaveGames")
    }

    It "succes : nouvelle save en place, plus aucun dossier .old / temporaire" {
        $backup = Backup-GameSave -Cfg $script:cfg -ServerCfg $script:srv -Kind "manual"
        Set-Content -LiteralPath (Join-Path $script:saveDir "Level.sav") -Value "donnees-corrompues"
        Set-Content -LiteralPath (Join-Path $script:saveDir "residu.sav") -Value "ne doit pas survivre"

        $detail = Restore-GameSave -Cfg $script:cfg -ServerCfg $script:srv -BackupFile $backup

        Get-Content -LiteralPath (Join-Path $script:saveDir "Level.sav") | Should -Be "donnees-monde-v1"
        Test-Path -LiteralPath (Join-Path $script:saveDir "residu.sav") | Should -BeFalse
        (Get-SiblingNames) | Should -Be @("SaveGames")
        $detail | Should -Match "pre-restore"
        Should -Invoke Start-GameServer -Times 1 -Exactly
    }

    It "dossier de saves absent au depart : restauration quand meme possible (pas de copie de surete)" {
        $backup = Backup-GameSave -Cfg $script:cfg -ServerCfg $script:srv -Kind "manual"
        Remove-Item -LiteralPath $script:saveDir -Recurse -Force

        $detail = Restore-GameSave -Cfg $script:cfg -ServerCfg $script:srv -BackupFile $backup

        Get-Content -LiteralPath (Join-Path $script:saveDir "Level.sav") | Should -Be "donnees-monde-v1"
        $detail | Should -Not -Match "pre-restore"
    }
}
