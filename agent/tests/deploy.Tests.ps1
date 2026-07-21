# Tests des ordres de deploiement (Lot 2 v2) : install_game / scan_exe.
# Conteneur Linux : TOUJOURS construire les chemins attendus via Join-Path,
# jamais de "\" en dur dans les assertions.
BeforeAll {
    . (Join-Path $PSScriptRoot ".." "hephaestos-lib.ps1")

    function New-FileWithSize {
        param([string]$Path, [int]$Bytes)
        $dir = Split-Path -Path $Path -Parent
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        [IO.File]::WriteAllBytes($Path, (New-Object byte[] $Bytes))
    }

    function New-InstallTree {
        # racine steamcmd factice : manifest + dossier d'install avec des exe
        param([string]$Root, [int]$AppId = 1829350, [string]$InstallDirName = "VRisingDedicatedServer")
        $steamapps = Join-Path $Root "steamapps"
        New-Item -ItemType Directory -Path $steamapps -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $steamapps "appmanifest_${AppId}.acf") -Value @"
"AppState"
{
    "appid"      "${AppId}"
    "name"       "V Rising Dedicated Server"
    "installdir" "${InstallDirName}"
    "buildid"    "42"
}
"@
        $installDir = Join-Path (Join-Path $steamapps "common") $InstallDirName
        New-FileWithSize -Path (Join-Path $installDir "VRisingServer.exe") -Bytes 5000
        New-FileWithSize -Path (Join-Path (Join-Path $installDir "tools") "Small.exe") -Bytes 100
        New-FileWithSize -Path (Join-Path $installDir "UnityCrashReporter.exe") -Bytes 9000
        New-FileWithSize -Path (Join-Path (Join-Path $installDir "redist") "vcredist_x64.exe") -Bytes 8000
        return $installDir
    }
}

Describe "Get-ExeCandidates" {
    It "retourne les exe relatifs tries par taille decroissante, exclusions filtrees" {
        $installDir = New-InstallTree -Root (Join-Path $TestDrive "steam")
        $result = @(Get-ExeCandidates -InstallDir $installDir)
        $result | Should -Be @("VRisingServer.exe", (Join-Path "tools" "Small.exe"))
    }

    It "plafonne a MaxCount" {
        $installDir = New-InstallTree -Root (Join-Path $TestDrive "steam2")
        foreach ($i in 1..40) {
            New-FileWithSize -Path (Join-Path $installDir "extra${i}.exe") -Bytes (200 + $i)
        }
        @(Get-ExeCandidates -InstallDir $installDir).Count | Should -Be 30
    }

    It "ignore les exe au-dela de la profondeur 4" {
        $installDir = New-InstallTree -Root (Join-Path $TestDrive "steam3")
        $deep = $installDir
        foreach ($d in 1..5) { $deep = Join-Path $deep "d${d}" }
        New-FileWithSize -Path (Join-Path $deep "TooDeep.exe") -Bytes 99999
        @(Get-ExeCandidates -InstallDir $installDir) | Should -Not -Contain (
            $deep.Substring($installDir.Length + 1) + [IO.Path]::DirectorySeparatorChar + "TooDeep.exe")
    }
}

Describe "Invoke-InstallGame" {
    It "steamcmd ok + manifest present + exe trouves -> ok avec candidats" {
        $root = Join-Path $TestDrive "steamok"
        $null = New-InstallTree -Root $root
        $cfg = [pscustomobject]@{ steamcmd = "steamcmd"; steamcmd_root = $root }
        Mock Invoke-Steamcmd { [pscustomobject]@{ ExitCode = 0; Output = "Success" } }
        $r = Invoke-InstallGame -Cfg $cfg -AppId 1829350
        $r.ok | Should -BeTrue
        @($r.exe_candidates)[0] | Should -Be "VRisingServer.exe"
        Should -Invoke Invoke-Steamcmd -Times 1 -ParameterFilter {
            # PAS de +force_install_dir (incident 14-15/07) : bibliotheque par defaut
            ($Arguments -join " ") -eq "+login anonymous +app_update 1829350 validate +quit"
        }
    }

    It "steamcmd en echec -> ok=false avec la fin de la sortie" {
        $cfg = [pscustomobject]@{ steamcmd = "steamcmd"; steamcmd_root = (Join-Path $TestDrive "x") }
        Mock Invoke-Steamcmd { [pscustomobject]@{ ExitCode = 8; Output = "Error! Missing game files" } }
        $r = Invoke-InstallGame -Cfg $cfg -AppId 1829350
        $r.ok | Should -BeFalse
        $r.detail | Should -Match "exit code 8"
    }

    It "steamcmd ok mais manifest absent -> ok=false (verification qui peut echouer)" {
        $root = Join-Path $TestDrive "nomanifest"
        New-Item -ItemType Directory -Path (Join-Path $root "steamapps") -Force | Out-Null
        $cfg = [pscustomobject]@{ steamcmd = "steamcmd"; steamcmd_root = $root }
        Mock Invoke-Steamcmd { [pscustomobject]@{ ExitCode = 0; Output = "Success" } }
        (Invoke-InstallGame -Cfg $cfg -AppId 1829350).ok | Should -BeFalse
    }

    It "aucun exe candidat -> ok=false" {
        $root = Join-Path $TestDrive "noexe"
        $installDir = New-InstallTree -Root $root
        Get-ChildItem -LiteralPath $installDir -Filter "*.exe" -Recurse | Remove-Item -Force
        $cfg = [pscustomobject]@{ steamcmd = "steamcmd"; steamcmd_root = $root }
        Mock Invoke-Steamcmd { [pscustomobject]@{ ExitCode = 0; Output = "Success" } }
        (Invoke-InstallGame -Cfg $cfg -AppId 1829350).ok | Should -BeFalse
    }
}

Describe "Invoke-ScanExe" {
    It "scan sans install : resout le dossier depuis le manifest" {
        $root = Join-Path $TestDrive "scan"
        $null = New-InstallTree -Root $root
        $cfg = [pscustomobject]@{ steamcmd = "steamcmd"; steamcmd_root = $root }
        $r = Invoke-ScanExe -Cfg $cfg -AppId 1829350
        $r.ok | Should -BeTrue
        @($r.exe_candidates)[0] | Should -Be "VRisingServer.exe"
    }

    It "manifest absent -> ok=false" {
        $cfg = [pscustomobject]@{ steamcmd = "steamcmd"; steamcmd_root = (Join-Path $TestDrive "vide") }
        (Invoke-ScanExe -Cfg $cfg -AppId 1829350).ok | Should -BeFalse
    }
}

Describe "Invoke-SetupServer" {
    BeforeEach {
        $script:root = Join-Path $TestDrive ("setup" + [guid]::NewGuid().ToString("N").Substring(0, 6))
        $script:installDir = New-InstallTree -Root $script:root
        $script:cfg = [pscustomobject]@{ steamcmd = "steamcmd"; steamcmd_root = $script:root }
        Mock New-GameStartTaskIfMissing { }
        Mock Start-GameServer { }
    }

    It "cree la tache planifiee avec l'exe resolu et demarre si start_now" {
        $order = [pscustomobject]@{ appid = 1829350; exe_path = "VRisingServer.exe"
            launch_args = "-persistentDataPath save"; task_name = "vrising"
            process = "VRisingServer"; start_now = $true }
        $r = Invoke-SetupServer -Cfg $script:cfg -Order $order
        $r.ok | Should -BeTrue
        Should -Invoke New-GameStartTaskIfMissing -Times 1 -ParameterFilter {
            $TaskName -eq "vrising" -and
            $Execute -eq (Join-Path $script:installDir "VRisingServer.exe") -and
            $Arguments -eq "-persistentDataPath save"
        }
        Should -Invoke Start-GameServer -Times 1 -ParameterFilter {
            $ServerCfg.start_task -eq "vrising" -and $ServerCfg.process -eq "VRisingServer"
        }
    }

    It "ne demarre pas sans start_now" {
        $order = [pscustomobject]@{ appid = 1829350; exe_path = "VRisingServer.exe"
            launch_args = ""; task_name = "vrising"; process = "VRisingServer"; start_now = $false }
        (Invoke-SetupServer -Cfg $script:cfg -Order $order).ok | Should -BeTrue
        Should -Invoke Start-GameServer -Times 0
    }

    It "refuse un exe_path qui sort du dossier d'install (anti-traversal)" {
        # conteneur Linux : traversal en separateurs natifs
        $evil = Join-Path (Join-Path ".." "..") "evil.exe"
        $order = [pscustomobject]@{ appid = 1829350; exe_path = $evil
            launch_args = ""; task_name = "vrising"; process = "evil"; start_now = $false }
        $r = Invoke-SetupServer -Cfg $script:cfg -Order $order
        $r.ok | Should -BeFalse
        $r.detail | Should -Match "hors du dossier"
        Should -Invoke New-GameStartTaskIfMissing -Times 0
    }

    It "refuse un exe inexistant" {
        $order = [pscustomobject]@{ appid = 1829350; exe_path = "Fantome.exe"
            launch_args = ""; task_name = "vrising"; process = "Fantome"; start_now = $false }
        (Invoke-SetupServer -Cfg $script:cfg -Order $order).ok | Should -BeFalse
    }
}
