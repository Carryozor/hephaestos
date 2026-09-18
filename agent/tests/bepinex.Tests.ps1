Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot/../hephaestos-lib.ps1"

    function New-TestZip {
        param([hashtable]$Entries, [string]$ZipPath)
        $stage = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $stage -Force | Out-Null
        foreach ($rel in $Entries.Keys) {
            $full = Join-Path $stage ($rel -replace "/", [IO.Path]::DirectorySeparatorChar)
            New-Item -ItemType Directory -Path (Split-Path $full -Parent) -Force | Out-Null
            Set-Content -LiteralPath $full -Value $Entries[$rel] -NoNewline
        }
        if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }
        Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $ZipPath -Force
        Remove-Item -LiteralPath $stage -Recurse -Force
    }

    function New-MaliciousZip {
        # Cree une entree zip avec un NOM LITTERAL arbitraire (contournant la
        # normalisation qu'imposerait un vrai chemin filesystem) -- seul moyen
        # de tester une vraie entree zip-slip/absolue, cf. revue securite H2.
        param([string]$ZipPath, [string]$EntryName, [string]$Content = "evil")
        if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }
        Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
        $zip = [System.IO.Compression.ZipFile]::Open($ZipPath, [System.IO.Compression.ZipArchiveMode]::Create)
        try {
            $entry = $zip.CreateEntry($EntryName)
            $stream = $entry.Open()
            try {
                $bytes = [Text.Encoding]::UTF8.GetBytes($Content)
                $stream.Write($bytes, 0, $bytes.Length)
            } finally {
                $stream.Dispose()
            }
        } finally {
            $zip.Dispose()
        }
    }
}

Describe "Test-BepInExUrlAllowed" {
    It "accepte un hote de l'allowlist en https" {
        { Test-BepInExUrlAllowed -Url "https://thunderstore.io/package/download/a/b/1.0.0/" } | Should -Not -Throw
        { Test-BepInExUrlAllowed -Url "https://gcdn.thunderstore.io/live/x.zip" } | Should -Not -Throw
        { Test-BepInExUrlAllowed -Url "https://github.com/a/b/releases/download/v1/x.zip" } | Should -Not -Throw
    }

    It "rejette http (non chiffre)" {
        { Test-BepInExUrlAllowed -Url "http://thunderstore.io/x.zip" } | Should -Throw
    }

    It "rejette un hote hors allowlist" {
        { Test-BepInExUrlAllowed -Url "https://evil.example.com/x.zip" } | Should -Throw
    }

    It "rejette une URL avec identifiants embarques" {
        { Test-BepInExUrlAllowed -Url "https://user:pass@thunderstore.io/x.zip" } | Should -Throw
    }
}

Describe "Expand-BepInExPackage" {
    BeforeAll {
        $script:zipDir = Join-Path $TestDrive "zips"
        New-Item -ItemType Directory -Path $script:zipDir -Force | Out-Null
    }

    It "extrait un zip plat (plugins/X.dll a la racine)" {
        $zip = Join-Path $script:zipDir "flat.zip"
        New-TestZip -ZipPath $zip -Entries @{
            "manifest.json" = "{}"
            "plugins/Jotunn.dll" = "binary-stub"
            "plugins/Jotunn.xml" = "<doc/>"
        }
        $dest = Join-Path $TestDrive "extract-flat"
        Expand-BepInExPackage -ZipPath $zip -DestDir $dest
        Test-Path (Join-Path $dest "plugins\Jotunn.dll") | Should -BeTrue
        Test-Path (Join-Path $dest "plugins\Jotunn.xml") | Should -BeTrue
    }

    It "extrait un zip avec sous-dossier (plugins/XPortal/XPortal.dll)" {
        $zip = Join-Path $script:zipDir "nested.zip"
        New-TestZip -ZipPath $zip -Entries @{
            "plugins/XPortal/XPortal.dll" = "binary-stub"
            "plugins/XPortal/Translations/en.json" = "{}"
        }
        $dest = Join-Path $TestDrive "extract-nested"
        Expand-BepInExPackage -ZipPath $zip -DestDir $dest
        Test-Path (Join-Path $dest "plugins\XPortal\XPortal.dll") | Should -BeTrue
        Test-Path (Join-Path $dest "plugins\XPortal\Translations\en.json") | Should -BeTrue
    }

    It "H2 : rejette une entree zip-slip (traversal) sans rien extraire hors de DestDir" {
        $zip = Join-Path $script:zipDir "slip.zip"
        New-MaliciousZip -ZipPath $zip -EntryName "plugins/../../evil.dll"
        $dest = Join-Path $TestDrive "extract-slip"
        { Expand-BepInExPackage -ZipPath $zip -DestDir $dest } | Should -Throw
        Test-Path (Join-Path $TestDrive "evil.dll") | Should -BeFalse
    }

    It "H2 : rejette une entree avec chemin absolu (lettre de lecteur Windows)" {
        $zip = Join-Path $script:zipDir "abs.zip"
        New-MaliciousZip -ZipPath $zip -EntryName "C:/evil.dll"
        $dest = Join-Path $TestDrive "extract-abs"
        { Expand-BepInExPackage -ZipPath $zip -DestDir $dest } | Should -Throw
    }

    It "H2 : rejette une entree avec chemin absolu (racine /)" {
        $zip = Join-Path $script:zipDir "root.zip"
        New-MaliciousZip -ZipPath $zip -EntryName "/etc/evil.dll"
        $dest = Join-Path $TestDrive "extract-rootabs"
        { Expand-BepInExPackage -ZipPath $zip -DestDir $dest } | Should -Throw
    }

    It "efface un dossier de destination preexistant avant d'extraire" {
        $dest = Join-Path $TestDrive "extract-clean"
        New-Item -ItemType Directory -Path $dest -Force | Out-Null
        "residu" | Set-Content -LiteralPath (Join-Path $dest "vieux.txt")
        $zip = Join-Path $script:zipDir "clean.zip"
        New-TestZip -ZipPath $zip -Entries @{ "plugins/Neuf.dll" = "x" }
        Expand-BepInExPackage -ZipPath $zip -DestDir $dest
        Test-Path (Join-Path $dest "vieux.txt") | Should -BeFalse
        Test-Path (Join-Path $dest "plugins\Neuf.dll") | Should -BeTrue
    }
}

Describe "Copy-BepInExPayload" {
    BeforeAll {
        $script:extractRoot = Join-Path $TestDrive "extracted-payload"
    }

    It "target plugins : copie plugins/* tel quel sous BepInEx/plugins/" {
        $serverDir = Join-Path $TestDrive "server-plugins-flat"
        New-Item -ItemType Directory -Path $serverDir -Force | Out-Null
        $extractDir = Join-Path $script:extractRoot "jotunn"
        New-Item -ItemType Directory -Path (Join-Path $extractDir "plugins") -Force | Out-Null
        "dll" | Set-Content -LiteralPath (Join-Path $extractDir "plugins\Jotunn.dll")
        "xml" | Set-Content -LiteralPath (Join-Path $extractDir "plugins\Jotunn.xml")

        $paths = Copy-BepInExPayload -ExtractedDir $extractDir -ServerDir $serverDir -Target "plugins" -Package "Jotunn"

        Test-Path (Join-Path $serverDir "BepInEx\plugins\Jotunn.dll") | Should -BeTrue
        Test-Path (Join-Path $serverDir "BepInEx\plugins\Jotunn.xml") | Should -BeTrue
        $paths | Should -Contain "BepInEx/plugins/Jotunn.dll"
        $paths | Should -Contain "BepInEx/plugins/Jotunn.xml"
    }

    It "target plugins : preserve un sous-dossier (XPortal)" {
        $serverDir = Join-Path $TestDrive "server-plugins-nested"
        New-Item -ItemType Directory -Path $serverDir -Force | Out-Null
        $extractDir = Join-Path $script:extractRoot "xportal"
        New-Item -ItemType Directory -Path (Join-Path $extractDir "plugins\XPortal") -Force | Out-Null
        "dll" | Set-Content -LiteralPath (Join-Path $extractDir "plugins\XPortal\XPortal.dll")

        $paths = Copy-BepInExPayload -ExtractedDir $extractDir -ServerDir $serverDir -Target "plugins" -Package "XPortal"

        Test-Path (Join-Path $serverDir "BepInEx\plugins\XPortal\XPortal.dll") | Should -BeTrue
        $paths | Should -Contain "BepInEx/plugins/XPortal"
    }

    It "target plugins : leve une exception si le package n'a pas de dossier plugins/" {
        $serverDir = Join-Path $TestDrive "server-plugins-missing"
        New-Item -ItemType Directory -Path $serverDir -Force | Out-Null
        $extractDir = Join-Path $script:extractRoot "sansplugins"
        New-Item -ItemType Directory -Path $extractDir -Force | Out-Null
        { Copy-BepInExPayload -ExtractedDir $extractDir -ServerDir $serverDir -Target "plugins" -Package "X" } | Should -Throw
    }

    It "target plugins : repli sur BepInEx/plugins/ si pas de plugins/ a la racine (ValheimPlus Grantapher, verifie sur le vrai zip Thunderstore le 18/09)" {
        $serverDir = Join-Path $TestDrive "server-plugins-nested-bepinex"
        New-Item -ItemType Directory -Path $serverDir -Force | Out-Null
        $extractDir = Join-Path $script:extractRoot "valheimplus"
        New-Item -ItemType Directory -Path (Join-Path $extractDir "BepInEx\plugins") -Force | Out-Null
        "dll" | Set-Content -LiteralPath (Join-Path $extractDir "BepInEx\plugins\ValheimPlus.dll")

        $paths = Copy-BepInExPayload -ExtractedDir $extractDir -ServerDir $serverDir -Target "plugins" -Package "ValheimPlus_Grantapher_Temporary"

        Test-Path (Join-Path $serverDir "BepInEx\plugins\ValheimPlus.dll") | Should -BeTrue
        $paths | Should -Contain "BepInEx/plugins/ValheimPlus.dll"
    }

    It "target root : copie BepInEx/core + winhttp.dll + doorstop_config.ini SANS toucher BepInEx/plugins existant" {
        $serverDir = Join-Path $TestDrive "server-root"
        New-Item -ItemType Directory -Path (Join-Path $serverDir "BepInEx\plugins") -Force | Out-Null
        "existant" | Set-Content -LiteralPath (Join-Path $serverDir "BepInEx\plugins\AutreMod.dll")

        $extractDir = Join-Path $script:extractRoot "bepinexpack\BepInExPack_Valheim"
        New-Item -ItemType Directory -Path (Join-Path $extractDir "BepInEx\core") -Force | Out-Null
        "preloader" | Set-Content -LiteralPath (Join-Path $extractDir "BepInEx\core\BepInEx.Preloader.dll")
        "ini" | Set-Content -LiteralPath (Join-Path $extractDir "doorstop_config.ini")
        "dll" | Set-Content -LiteralPath (Join-Path $extractDir "winhttp.dll")

        $paths = Copy-BepInExPayload -ExtractedDir (Join-Path $script:extractRoot "bepinexpack") -ServerDir $serverDir `
            -Target "root" -Package "BepInExPack_Valheim"

        Test-Path (Join-Path $serverDir "BepInEx\core\BepInEx.Preloader.dll") | Should -BeTrue
        Test-Path (Join-Path $serverDir "winhttp.dll") | Should -BeTrue
        Test-Path (Join-Path $serverDir "doorstop_config.ini") | Should -BeTrue
        Test-Path (Join-Path $serverDir "BepInEx\plugins\AutreMod.dll") | Should -BeTrue
        $paths | Should -Contain "BepInEx/core"
    }
}

Describe "Remove-BepInExPaths" {
    It "supprime un fichier et un dossier existants" {
        $serverDir = Join-Path $TestDrive "server-remove"
        New-Item -ItemType Directory -Path (Join-Path $serverDir "BepInEx\plugins\XPortal") -Force | Out-Null
        "x" | Set-Content -LiteralPath (Join-Path $serverDir "BepInEx\plugins\Jotunn.dll")
        "y" | Set-Content -LiteralPath (Join-Path $serverDir "BepInEx\plugins\XPortal\XPortal.dll")

        Remove-BepInExPaths -ServerDir $serverDir -Paths @("BepInEx/plugins/Jotunn.dll", "BepInEx/plugins/XPortal")

        Test-Path (Join-Path $serverDir "BepInEx\plugins\Jotunn.dll") | Should -BeFalse
        Test-Path (Join-Path $serverDir "BepInEx\plugins\XPortal") | Should -BeFalse
    }

    It "ne fait rien (no-op) sur un chemin absent" {
        $serverDir = Join-Path $TestDrive "server-remove-noop"
        New-Item -ItemType Directory -Path $serverDir -Force | Out-Null
        { Remove-BepInExPaths -ServerDir $serverDir -Paths @("BepInEx/plugins/Absent.dll") } | Should -Not -Throw
    }

    It "leve une exception sur un chemin de traversal" {
        $serverDir = Join-Path $TestDrive "server-remove-traversal"
        { Remove-BepInExPaths -ServerDir $serverDir -Paths @("BepInEx/plugins/../../evil.dll") } | Should -Throw
    }

    It "leve une exception sur un chemin hors de BepInEx/plugins" {
        $serverDir = Join-Path $TestDrive "server-remove-outside"
        { Remove-BepInExPaths -ServerDir $serverDir -Paths @("winhttp.dll") } | Should -Throw
    }

    It "leve une exception sur un chemin absolu" {
        $serverDir = Join-Path $TestDrive "server-remove-absolute"
        { Remove-BepInExPaths -ServerDir $serverDir -Paths @("/etc/passwd") } | Should -Throw
    }
}

Describe "Get-BepInExLogStatus" {
    BeforeAll {
        $script:logDir = Join-Path $TestDrive "logdir"
        New-Item -ItemType Directory -Path $script:logDir -Force | Out-Null
    }

    It "retourne ChainloaderComplete=false et Plugins vide si le fichier est absent" {
        $status = Get-BepInExLogStatus -LogPath (Join-Path $script:logDir "absent.log") -MinWriteTimeUtc (Get-Date).ToUniversalTime()
        $status.ChainloaderComplete | Should -BeFalse
        $status.Plugins | Should -BeNullOrEmpty
    }

    It "parse les plugins charges et confirme le chainloader" {
        $log = Join-Path $script:logDir "boot-ok.log"
        @"
[Info   :   BepInEx] 4 plugins to load
[Info   :   BepInEx] Loading [Jotunn 2.30.0]
[Info   :   BepInEx] Loading [Valheim Plus 0.10.1.1]
[Info   :   BepInEx] Loading [Equipment and Quick Slots 3.1.2]
[Info   :   BepInEx] Loading [XPortal 1.2.25]
[Message:   BepInEx] Chainloader startup complete
"@ | Set-Content -LiteralPath $log
        $status = Get-BepInExLogStatus -LogPath $log -MinWriteTimeUtc ((Get-Date).ToUniversalTime().AddMinutes(-5))
        $status.ChainloaderComplete | Should -BeTrue
        ($status.Plugins | Where-Object { $_.Name -eq "Jotunn" }).Version | Should -Be "2.30.0"
        ($status.Plugins | Where-Object { $_.Name -eq "Equipment and Quick Slots" }).Version | Should -Be "3.1.2"
        $status.Plugins.Count | Should -Be 4
    }

    It "P2 : ignore un log dont la derniere ecriture precede MinWriteTimeUtc (vieux boot reussi)" {
        $log = Join-Path $script:logDir "boot-old.log"
        "[Message:   BepInEx] Chainloader startup complete" | Set-Content -LiteralPath $log
        $future = (Get-Item -LiteralPath $log).LastWriteTimeUtc.AddMinutes(5)
        $status = Get-BepInExLogStatus -LogPath $log -MinWriteTimeUtc $future
        $status.ChainloaderComplete | Should -BeFalse
    }

    It "P3 : des [Error] cosmetiques n'empechent pas la confirmation" {
        $log = Join-Path $script:logDir "boot-errors.log"
        @"
[Error  : Unity Log] AsyncResourceUpload failed.
[Info   :   BepInEx] Loading [Jotunn 2.30.0]
[Message:   BepInEx] Chainloader startup complete
[Error  : Unity Log] Failed to play intro cinematic
"@ | Set-Content -LiteralPath $log
        $status = Get-BepInExLogStatus -LogPath $log -MinWriteTimeUtc ((Get-Date).ToUniversalTime().AddMinutes(-5))
        $status.ChainloaderComplete | Should -BeTrue
    }
}

Describe "Invoke-BepInExDownload" {
    It "rejette une URL non autorisee AVANT toute requete" {
        Mock Invoke-WebRequest { throw "ne doit jamais etre appele" }
        { Invoke-BepInExDownload -Url "https://evil.example.com/x.zip" -DestPath (Join-Path $TestDrive "x.zip") } | Should -Throw
        Should -Invoke Invoke-WebRequest -Times 0 -Exactly
    }

    It "leve une exception si le fichier n'est pas produit" {
        Mock Invoke-WebRequest {}
        { Invoke-BepInExDownload -Url "https://thunderstore.io/x.zip" -DestPath (Join-Path $TestDrive "jamais-cree.zip") } | Should -Throw
    }

    It "rejette et supprime un fichier au-dela de la taille maximale" {
        $dest = Join-Path $TestDrive "trop-gros.zip"
        Mock Invoke-WebRequest {
            # simule un vrai telechargement : le fichier existe au retour de la commande
            [byte[]]::new(1) | Set-Content -LiteralPath $dest -Encoding Byte
        }
        Mock Get-Item { [pscustomobject]@{ Length = 200MB } } -ParameterFilter { $LiteralPath -eq $dest }
        { Invoke-BepInExDownload -Url "https://thunderstore.io/x.zip" -DestPath $dest } | Should -Throw
    }

    It "H1 : rejette et supprime le fichier si la redirection atterrit hors allowlist (ResponseUri, style Windows PowerShell 5.1)" {
        $dest = Join-Path $TestDrive "redirige-mauvais-hote.zip"
        Mock Invoke-WebRequest {
            "contenu" | Set-Content -LiteralPath $dest
            [pscustomobject]@{ BaseResponse = [pscustomobject]@{ ResponseUri = [Uri]"https://evil.example.com/x.zip" } }
        }
        { Invoke-BepInExDownload -Url "https://thunderstore.io/x.zip" -DestPath $dest } | Should -Throw
        Test-Path -LiteralPath $dest | Should -BeFalse
    }

    It "H1 : rejette si la redirection atterrit hors allowlist (RequestMessage.RequestUri, style PowerShell 7+)" {
        $dest = Join-Path $TestDrive "redirige-mauvais-hote-ps7.zip"
        Mock Invoke-WebRequest {
            "contenu" | Set-Content -LiteralPath $dest
            [pscustomobject]@{ BaseResponse = [pscustomobject]@{
                RequestMessage = [pscustomobject]@{ RequestUri = [Uri]"https://evil.example.com/x.zip" } } }
        }
        { Invoke-BepInExDownload -Url "https://thunderstore.io/x.zip" -DestPath $dest } | Should -Throw
        Test-Path -LiteralPath $dest | Should -BeFalse
    }

    It "H1 : une redirection vers un hote de l'allowlist (CDN) n'est PAS rejetee" {
        $dest = Join-Path $TestDrive "redirige-bon-hote.zip"
        Mock Invoke-WebRequest {
            "contenu" | Set-Content -LiteralPath $dest
            [pscustomobject]@{ BaseResponse = [pscustomobject]@{ ResponseUri = [Uri]"https://gcdn.thunderstore.io/x.zip" } }
        }
        { Invoke-BepInExDownload -Url "https://thunderstore.io/x.zip" -DestPath $dest } | Should -Not -Throw
        Test-Path -LiteralPath $dest | Should -BeTrue
    }
}

Describe "Backup-BepInExState / Restore-BepInExBackup" {
    <#
    Revue qualite du 12/09/2026 (MEDIUM) : Update-BepInExMods mocke entierement
    ces deux fonctions dans tous ses tests -- le vrai zip/dezip/rotation n'etait
    jamais exerce. Tests reels ci-dessous (vrai Compress-Archive/Expand-Archive
    sur TestDrive, aucun mock), c'est exactement la partie "bloquante, sans elle
    pas de rollback possible".
    #>
    BeforeAll {
        function New-BepinexServerDir {
            param([string]$Root)
            New-Item -ItemType Directory -Path (Join-Path $Root "BepInEx\plugins") -Force | Out-Null
            New-Item -ItemType Directory -Path (Join-Path $Root "BepInEx\core") -Force | Out-Null
            "preloader-v1" | Set-Content -LiteralPath (Join-Path $Root "BepInEx\core\BepInEx.Preloader.dll")
            "jotunn-v1" | Set-Content -LiteralPath (Join-Path $Root "BepInEx\plugins\Jotunn.dll")
            "winhttp-v1" | Set-Content -LiteralPath (Join-Path $Root "winhttp.dll")
            "doorstop-v1" | Set-Content -LiteralPath (Join-Path $Root "doorstop_config.ini")
        }
    }

    It "cree un zip reel contenant BepInEx/, winhttp.dll et doorstop_config.ini" {
        $serverDir = Join-Path $TestDrive "backup-real-server"
        New-BepinexServerDir -Root $serverDir
        $cfg = [pscustomobject]@{ steamcmd_root = (Join-Path $TestDrive "backup-real-steam") }
        $serverCfg = [pscustomobject]@{ name = "valheim" }

        $zipPath = Backup-BepInExState -Cfg $cfg -ServerCfg $serverCfg -ServerDir $serverDir

        Test-Path -LiteralPath $zipPath | Should -BeTrue
        $extractCheck = Join-Path $TestDrive "backup-real-check"
        Expand-Archive -LiteralPath $zipPath -DestinationPath $extractCheck
        Get-Content -LiteralPath (Join-Path $extractCheck "BepInEx\core\BepInEx.Preloader.dll") | Should -Be "preloader-v1"
        Get-Content -LiteralPath (Join-Path $extractCheck "BepInEx\plugins\Jotunn.dll") | Should -Be "jotunn-v1"
        Get-Content -LiteralPath (Join-Path $extractCheck "winhttp.dll") | Should -Be "winhttp-v1"
        Get-Content -LiteralPath (Join-Path $extractCheck "doorstop_config.ini") | Should -Be "doorstop-v1"
    }

    It "ne pollue pas le dossier de backups de save (dossier dedie '<serveur>-bepinex')" {
        $serverDir = Join-Path $TestDrive "backup-noleak-server"
        New-BepinexServerDir -Root $serverDir
        $cfg = [pscustomobject]@{ steamcmd_root = (Join-Path $TestDrive "backup-noleak-steam") }
        $serverCfg = [pscustomobject]@{ name = "valheim" }

        $zipPath = Backup-BepInExState -Cfg $cfg -ServerCfg $serverCfg -ServerDir $serverDir

        $zipPath | Should -Match ([regex]::Escape((Join-Path $cfg.steamcmd_root "hephaestos-backups\valheim-bepinex")))
        (Get-SaveBackupDir -Cfg $cfg -ServerCfg $serverCfg) | Should -Not -Be (Split-Path $zipPath -Parent)
    }

    It "reussit meme si doorstop_config.ini est absent (pack pas encore installe)" {
        $serverDir = Join-Path $TestDrive "backup-partial-server"
        New-Item -ItemType Directory -Path (Join-Path $serverDir "BepInEx\plugins") -Force | Out-Null
        "jotunn-only" | Set-Content -LiteralPath (Join-Path $serverDir "BepInEx\plugins\Jotunn.dll")
        $cfg = [pscustomobject]@{ steamcmd_root = (Join-Path $TestDrive "backup-partial-steam") }
        $serverCfg = [pscustomobject]@{ name = "valheim" }

        { Backup-BepInExState -Cfg $cfg -ServerCfg $serverCfg -ServerDir $serverDir } | Should -Not -Throw
    }

    It "purge au-dela de 5 backups conserves (le plus ancien disparait)" {
        $serverDir = Join-Path $TestDrive "backup-rotation-server"
        New-BepinexServerDir -Root $serverDir
        $cfg = [pscustomobject]@{ steamcmd_root = (Join-Path $TestDrive "backup-rotation-steam") }
        $serverCfg = [pscustomobject]@{ name = "valheim" }
        $backupDir = Get-BepInExBackupDir -Cfg $cfg -ServerCfg $serverCfg
        New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
        # 5 backups "anciens" pre-crees (noms anterieurs par tri lexicographique) :
        # Backup-BepInExState en cree un 6e, le plus ancien des 6 doit disparaitre.
        foreach ($i in 1..5) {
            "x" | Set-Content -LiteralPath (Join-Path $backupDir "2020010$i-000000-bepinex.zip")
        }

        Backup-BepInExState -Cfg $cfg -ServerCfg $serverCfg -ServerDir $serverDir | Out-Null

        $remaining = Get-ChildItem -LiteralPath $backupDir -Filter "*.zip" | Sort-Object Name
        $remaining.Count | Should -Be 5
        $remaining.Name | Should -Not -Contain "20200101-000000-bepinex.zip"
    }

    It "Restore-BepInExBackup remet en place le contenu d'un backup precedent" {
        $serverDir = Join-Path $TestDrive "restore-real-server"
        New-BepinexServerDir -Root $serverDir
        $cfg = [pscustomobject]@{ steamcmd_root = (Join-Path $TestDrive "restore-real-steam") }
        $serverCfg = [pscustomobject]@{ name = "valheim" }
        $zipPath = Backup-BepInExState -Cfg $cfg -ServerCfg $serverCfg -ServerDir $serverDir

        # simule une MAJ qui a modifie/casse les fichiers
        "jotunn-v2-corrompu" | Set-Content -LiteralPath (Join-Path $serverDir "BepInEx\plugins\Jotunn.dll")
        "winhttp-v2" | Set-Content -LiteralPath (Join-Path $serverDir "winhttp.dll")

        Restore-BepInExBackup -BackupZipPath $zipPath -ServerDir $serverDir

        Get-Content -LiteralPath (Join-Path $serverDir "BepInEx\plugins\Jotunn.dll") | Should -Be "jotunn-v1"
        Get-Content -LiteralPath (Join-Path $serverDir "winhttp.dll") | Should -Be "winhttp-v1"
    }

    It "Restore-BepInExBackup leve une exception explicite si le zip est introuvable" {
        $serverDir = Join-Path $TestDrive "restore-missing-server"
        New-Item -ItemType Directory -Path $serverDir -Force | Out-Null
        { Restore-BepInExBackup -BackupZipPath (Join-Path $TestDrive "jamais-cree.zip") -ServerDir $serverDir } | Should -Throw
    }
}

Describe "Update-BepInExMods" {
    BeforeAll {
        $script:serverCfgBepinex = [pscustomobject]@{
            name = "valheim"; appid = 896660; process = "valheim_server"
            stop_adapter = "generic-graceful"; start_task = "valheim"
        }
        $script:cfgBepinex = [pscustomobject]@{ steamcmd_root = (Join-Path $TestDrive "steam-bepinex") }
    }

    BeforeEach {
        Mock Get-ServerInstallDir { Join-Path $TestDrive "server-install" }
        Mock Get-GameSaveDir { $null }
        Mock Stop-GameServer {}
        Mock Start-GameServer {}
        Mock Backup-BepInExState { "fake-backup.zip" }
        Mock Restore-BepInExBackup {}
        Mock Invoke-BepInExDownload {}
        Mock Expand-BepInExPackage { Join-Path $TestDrive "extracted" }
        Mock Copy-BepInExPayload { @("BepInEx/plugins/Jotunn.dll") }
        Mock Remove-BepInExPaths {}
        Mock Get-BepInExLogStatus {
            [pscustomobject]@{ ChainloaderComplete = $true; Plugins = @([pscustomobject]@{ Name = "Jotunn"; Version = "2.31.0" }) }
        }

        function New-JotunnMod {
            [pscustomobject]@{
                slug = "ValheimModding/Jotunn"; package = "Jotunn"; version = "2.31.0"
                download_url = "https://thunderstore.io/package/download/ValheimModding/Jotunn/2.31.0/"
                target = "plugins"; previous_paths = @("BepInEx/plugins/Jotunn.dll", "BepInEx/plugins/Jotunn.xml")
                expected_plugin = "Jotunn"
            }
        }
    }

    It "chemin nominal : ok=true, sequence complete respectee, bepinex_installed peuple" {
        $result = Update-BepInExMods -Cfg $script:cfgBepinex -ServerCfg $script:serverCfgBepinex -Mods @(New-JotunnMod)
        $result.ok | Should -BeTrue
        $result.bepinex_installed[0].slug | Should -Be "ValheimModding/Jotunn"
        Should -Invoke Stop-GameServer -Times 1 -Exactly
        Should -Invoke Backup-BepInExState -Times 1 -Exactly
        Should -Invoke Remove-BepInExPaths -Times 1 -Exactly
        Should -Invoke Start-GameServer -Times 1 -Exactly
        Should -Invoke Restore-BepInExBackup -Times 0 -Exactly
    }

    It "P8 : un echec de preparation (telechargement/extraction) ne doit JAMAIS arreter le serveur" {
        # Revue qualite 12/09 : ce test verifie P8 (l'ordre stop/backup/copie
        # n'est jamais atteint si la preparation echoue), PAS l'allowlist
        # d'URL elle-meme (deja couverte par le Describe "Test-BepInExUrlAllowed"
        # et par Invoke-BepInExDownload -- Mock ici simule n'importe quel echec
        # de preparation, dont un rejet d'URL en ferait partie en conditions reelles).
        Mock Invoke-BepInExDownload { throw "hote non autorise" }
        $result = Update-BepInExMods -Cfg $script:cfgBepinex -ServerCfg $script:serverCfgBepinex -Mods @(New-JotunnMod)
        $result.ok | Should -BeFalse
        Should -Invoke Stop-GameServer -Times 0 -Exactly
    }

    It "echec de telechargement sur un mod parmi plusieurs : rien n'est installe, serveur pas arrete" {
        $xportalMod = [pscustomobject]@{
            slug = "SpikeHimself/XPortal"; package = "XPortal"; version = "1.2.26"
            download_url = "https://github.com/SpikeHimself/XPortal/releases/download/v1.2.26/XPortal-v1.2.26.zip"
            target = "plugins"; previous_paths = @("BepInEx/plugins/XPortal"); expected_plugin = "XPortal"
        }
        $mods = @((New-JotunnMod), $xportalMod)
        Mock Invoke-BepInExDownload { throw "timeout" } -ParameterFilter { $Url -like "*XPortal*" }
        $result = Update-BepInExMods -Cfg $script:cfgBepinex -ServerCfg $script:serverCfgBepinex -Mods $mods
        $result.ok | Should -BeFalse
        Should -Invoke Stop-GameServer -Times 0 -Exactly
        Should -Invoke Copy-BepInExPayload -Times 0 -Exactly
    }

    It "backup BepInEx bloquant : un echec de backup annule tout, serveur redemarre sans MAJ" {
        Mock Backup-BepInExState { throw "disque plein" }
        $result = Update-BepInExMods -Cfg $script:cfgBepinex -ServerCfg $script:serverCfgBepinex -Mods @(New-JotunnMod)
        $result.ok | Should -BeFalse
        Should -Invoke Copy-BepInExPayload -Times 0 -Exactly
        Should -Invoke Start-GameServer -Times 1 -Exactly
    }

    It "chainloader jamais confirme apres redemarrage : rollback + restart, ok=false" {
        Mock Get-BepInExLogStatus { [pscustomobject]@{ ChainloaderComplete = $false; Plugins = @() } }
        Mock Start-Sleep {}
        $result = Update-BepInExMods -Cfg $script:cfgBepinex -ServerCfg $script:serverCfgBepinex -Mods @(New-JotunnMod)
        $result.ok | Should -BeFalse
        Should -Invoke Restore-BepInExBackup -Times 1 -Exactly
        Should -Invoke Start-GameServer -Times 2 -Exactly  # 1er demarrage + redemarrage post-rollback
    }

    It "plugin attendu absent du chainloader malgre un chainloader complet : rollback, ok=false" {
        Mock Get-BepInExLogStatus {
            [pscustomobject]@{ ChainloaderComplete = $true; Plugins = @([pscustomobject]@{ Name = "AutreMod"; Version = "1.0.0" }) }
        }
        $result = Update-BepInExMods -Cfg $script:cfgBepinex -ServerCfg $script:serverCfgBepinex -Mods @(New-JotunnMod)
        $result.ok | Should -BeFalse
        Should -Invoke Restore-BepInExBackup -Times 1 -Exactly
    }

    It "Start-GameServer qui echoue pendant le rollback est capture (piege F2)" {
        Mock Get-BepInExLogStatus { [pscustomobject]@{ ChainloaderComplete = $false; Plugins = @() } }
        Mock Start-Sleep {}
        Mock Start-GameServer { throw "process ne remonte pas" }
        { Update-BepInExMods -Cfg $script:cfgBepinex -ServerCfg $script:serverCfgBepinex -Mods @(New-JotunnMod) } | Should -Not -Throw
        $result = Update-BepInExMods -Cfg $script:cfgBepinex -ServerCfg $script:serverCfgBepinex -Mods @(New-JotunnMod)
        $result.ok | Should -BeFalse
    }
}
