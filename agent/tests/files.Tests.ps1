# Tests des ordres d'edition de fichiers (Lot 3 v2) : list_files / read_file / write_file.
BeforeAll {
    . (Join-Path $PSScriptRoot ".." "hephaestos-lib.ps1")

    function New-ConfigTree {
        param([string]$Root)
        $steamapps = Join-Path $Root "steamapps"
        $installDir = Join-Path (Join-Path $steamapps "common") "PalServer"
        New-Item -ItemType Directory -Path $installDir -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $installDir "Settings.ini") -Value "key=value"
        $sub = Join-Path $installDir "WindowsServer"
        New-Item -ItemType Directory -Path $sub -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $sub "Extra.cfg") -Value "extra=1"
        Set-Content -LiteralPath (Join-Path $installDir "server.exe") -Value "binaire"
        return $installDir
    }

    function New-Manifest {
        param([string]$Root, [int]$AppId = 1)
        New-Item -ItemType Directory -Path (Join-Path $Root "steamapps") -Force | Out-Null
        Set-Content -LiteralPath (Join-Path (Join-Path $Root "steamapps") "appmanifest_${AppId}.acf") -Value @"
"AppState"
{ "appid" "${AppId}" "installdir" "PalServer" }
"@
    }
}

Describe "Get-ConfigRoot" {
    It "resout la racine install via le manifest" {
        $root = Join-Path $TestDrive "gcr1"
        $installDir = New-ConfigTree -Root $root
        New-Manifest -Root $root
        $cfg = [pscustomobject]@{ steamcmd_root = $root }
        $serverCfg = [pscustomobject]@{ appid = 1 }
        Get-ConfigRoot -Cfg $cfg -ServerCfg $serverCfg -Root "install" | Should -Be $installDir
    }

    It "leve une exception si root=save sans save_dir configure" {
        $cfg = [pscustomobject]@{ steamcmd_root = $TestDrive }
        $serverCfg = [pscustomobject]@{ appid = 1 }
        { Get-ConfigRoot -Cfg $cfg -ServerCfg $serverCfg -Root "save" } | Should -Throw "*save_dir*"
    }

    It "resout save_dir relatif a l'install (coherent avec Get-GameSaveDir/backups, jamais tel quel)" {
        # Trouve en revue finale (18/07) : save_dir est TOUJOURS relatif au dossier
        # d'install partout ailleurs (Get-GameSaveDir, backups, libelle du formulaire
        # de finalisation "relatif a l'install") -- Get-ConfigRoot doit suivre la
        # meme convention, pas le renvoyer tel quel (qui casse la racine "save" pour
        # tout save_dir relatif, le cas normal Palworld/Windrose).
        $root = Join-Path $TestDrive "gcr2"
        $installDir = New-ConfigTree -Root $root
        New-Manifest -Root $root
        $cfg = [pscustomobject]@{ steamcmd_root = $root }
        $serverCfg = [pscustomobject]@{ appid = 1; save_dir = "Saved" }
        Get-ConfigRoot -Cfg $cfg -ServerCfg $serverCfg -Root "save" | Should -Be (Join-Path $installDir "Saved")
    }
}

Describe "Invoke-ListFiles" {
    It "liste les fichiers de la whitelist, chemins relatifs en '/' , exclut les extensions hors whitelist" {
        $root = Join-Path $TestDrive "list1"
        $installDir = New-ConfigTree -Root $root
        New-Manifest -Root $root
        $cfg = [pscustomobject]@{ steamcmd_root = $root }
        $serverCfg = [pscustomobject]@{ appid = 1 }
        $r = Invoke-ListFiles -Cfg $cfg -ServerCfg $serverCfg -Root "install"
        $r.ok | Should -BeTrue
        $r.files | Should -Contain "Settings.ini"
        $r.files | Should -Contain "WindowsServer/Extra.cfg"
        $r.files | Should -Not -Contain "server.exe"
    }

    It "plafonne a 500 entrees" {
        $root = Join-Path $TestDrive "list2"
        $installDir = New-ConfigTree -Root $root
        foreach ($i in 1..520) { Set-Content -LiteralPath (Join-Path $installDir "f${i}.ini") -Value "x" }
        New-Manifest -Root $root
        $cfg = [pscustomobject]@{ steamcmd_root = $root }
        (Invoke-ListFiles -Cfg $cfg -ServerCfg ([pscustomobject]@{ appid = 1 }) -Root "install").files.Count | Should -Be 500
    }

    It "dossier introuvable -> ok=false" {
        $cfg = [pscustomobject]@{ steamcmd_root = (Join-Path $TestDrive "vide") }
        (Invoke-ListFiles -Cfg $cfg -ServerCfg ([pscustomobject]@{ appid = 1 }) -Root "install").ok | Should -BeFalse
    }
}

Describe "Invoke-ReadFile" {
    It "lit un fichier de la whitelist et calcule son sha256" {
        $root = Join-Path $TestDrive "read1"
        $installDir = New-ConfigTree -Root $root
        New-Manifest -Root $root
        $cfg = [pscustomobject]@{ steamcmd_root = $root }
        $r = Invoke-ReadFile -Cfg $cfg -ServerCfg ([pscustomobject]@{ appid = 1 }) -Root "install" -Path "Settings.ini"
        $r.ok | Should -BeTrue
        [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($r.content_b64)).Trim() | Should -Be "key=value"
        $r.sha256 | Should -Match "^[0-9a-f]{64}$"
    }

    It "refuse un chemin hors du dossier d'install (anti-traversal)" {
        $root = Join-Path $TestDrive "read2"
        $installDir = New-ConfigTree -Root $root
        New-Manifest -Root $root
        $cfg = [pscustomobject]@{ steamcmd_root = $root }
        $evil = Join-Path (Join-Path ".." "..") "evil.ini"
        $r = Invoke-ReadFile -Cfg $cfg -ServerCfg ([pscustomobject]@{ appid = 1 }) -Root "install" -Path $evil
        $r.ok | Should -BeFalse
        $r.detail | Should -Match "hors du dossier"
    }

    It "fichier absent -> ok=false" {
        $root = Join-Path $TestDrive "read3"
        New-ConfigTree -Root $root | Out-Null
        New-Manifest -Root $root
        $cfg = [pscustomobject]@{ steamcmd_root = $root }
        (Invoke-ReadFile -Cfg $cfg -ServerCfg ([pscustomobject]@{ appid = 1 }) -Root "install" -Path "Fantome.ini").ok | Should -BeFalse
    }
}

Describe "Invoke-WriteFile" {
    BeforeEach {
        $script:root = Join-Path $TestDrive ("write" + [guid]::NewGuid().ToString("N").Substring(0, 6))
        $script:installDir = New-ConfigTree -Root $script:root
        New-Manifest -Root $script:root
        $script:cfg = [pscustomobject]@{ steamcmd_root = $script:root }
        $script:serverCfg = [pscustomobject]@{ appid = 1 }
    }

    It "ecrit le contenu si le sha256 correspond, cree .hephaestos-bak, ecriture atomique" {
        $target = Join-Path $script:installDir "Settings.ini"
        $before = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLower()
        $newContent = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("key=nouvelle_valeur"))
        $r = Invoke-WriteFile -Cfg $script:cfg -ServerCfg $script:serverCfg -Root "install" `
            -Path "Settings.ini" -ContentB64 $newContent -ExpectedSha256 $before
        $r.ok | Should -BeTrue
        Get-Content -LiteralPath $target -Raw | Should -Match "nouvelle_valeur"
        Test-Path -LiteralPath "${target}.hephaestos-bak" | Should -BeTrue
        (Get-Content -LiteralPath "${target}.hephaestos-bak" -Raw) | Should -Match "key=value"
        Test-Path -LiteralPath "${target}.tmp" | Should -BeFalse
    }

    It "refuse l'ecriture si le sha256 ne correspond pas (conflit), ne cree pas .hephaestos-bak" {
        $target = Join-Path $script:installDir "Settings.ini"
        $newContent = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("hack"))
        $r = Invoke-WriteFile -Cfg $script:cfg -ServerCfg $script:serverCfg -Root "install" `
            -Path "Settings.ini" -ContentB64 $newContent -ExpectedSha256 ("0" * 64)
        $r.ok | Should -BeFalse
        $r.detail | Should -Match "change depuis"
        Get-Content -LiteralPath $target -Raw | Should -Match "key=value"  # inchange
        Test-Path -LiteralPath "${target}.hephaestos-bak" | Should -BeFalse
    }

    It "refuse un chemin hors du dossier d'install (anti-traversal)" {
        $newContent = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("x"))
        $evil = Join-Path (Join-Path ".." "..") "evil.ini"
        $r = Invoke-WriteFile -Cfg $script:cfg -ServerCfg $script:serverCfg -Root "install" `
            -Path $evil -ContentB64 $newContent -ExpectedSha256 ("0" * 64)
        $r.ok | Should -BeFalse
        $r.detail | Should -Match "hors du dossier"
    }

    It "fichier absent -> ok=false, pas de creation" {
        $newContent = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("x"))
        $r = Invoke-WriteFile -Cfg $script:cfg -ServerCfg $script:serverCfg -Root "install" `
            -Path "Fantome.ini" -ContentB64 $newContent -ExpectedSha256 ("0" * 64)
        $r.ok | Should -BeFalse
        Test-Path -LiteralPath (Join-Path $script:installDir "Fantome.ini") | Should -BeFalse
    }
}
