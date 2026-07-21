Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot/../hephaestos-lib.ps1"
}

Describe "Get-InstalledWorkshopMods" {
    BeforeAll {
        $script:steamRootMods = Join-Path $TestDrive "steam-mods"
        $script:manifestDirMods = Join-Path $script:steamRootMods "steamapps"
        New-Item -ItemType Directory -Path $script:manifestDirMods -Force | Out-Null
        @'
"AppState"
{
	"appid"		"2394010"
	"installdir"		"PalServer"
	"buildid"		"100"
}
'@ | Set-Content -LiteralPath (Join-Path $script:manifestDirMods "appmanifest_2394010.acf") -Encoding UTF8

        $script:workshopDirMods = Join-Path $script:steamRootMods "steamapps\common\PalServer\Mods\Workshop"
        $script:cfgMods = [pscustomobject]@{ steamcmd_root = $script:steamRootMods; steamcmd = "C:\steam\steamcmd.exe" }
        $script:serverCfgMods = [pscustomobject]@{ appid = 2394010; workshop_appid = 1623730 }
    }

    It "retourne un tableau vide quand le dossier Mods\Workshop n'existe pas" {
        Get-InstalledWorkshopMods -Cfg $script:cfgMods -ServerCfg $script:serverCfgMods | Should -BeNullOrEmpty
    }

    It "retourne les noms des sous-dossiers presents" {
        New-Item -ItemType Directory -Path (Join-Path $script:workshopDirMods "3147025543") -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $script:workshopDirMods "999") -Force | Out-Null

        $result = @(Get-InstalledWorkshopMods -Cfg $script:cfgMods -ServerCfg $script:serverCfgMods)

        $result.Count | Should -Be 2
        $result | Should -Contain "3147025543"
        $result | Should -Contain "999"
    }
}

Describe "Install-WorkshopMod" {
    BeforeAll {
        # Stub steamcmd : meme convention que agent/tests/update.Tests.ps1 (script .ps1
        # execute comme un vrai binaire via $Cfg.steamcmd, pas de Mock sur l'appel natif).
        # Cree le dossier workshop/content/<workshop_appid>/<id> comme le ferait un vrai
        # +workshop_download_item, sauf si HEPHAESTOS_TEST_WORKSHOP_NO_DOWNLOAD est positionne
        # (simule un ID invalide/inexistant qui ne produit aucun contenu).
        $script:stubPathInstall = Join-Path $TestDrive "steamcmd_stub_install.ps1"
        @'
param()

$workshopAppid = $null
$workshopId = $null
$loginArg = $null
for ($i = 0; $i -lt $args.Count; $i++) {
    if ($args[$i] -eq "+workshop_download_item") {
        $workshopAppid = $args[$i + 1]
        $workshopId = $args[$i + 2]
    }
    if ($args[$i] -eq "+login") {
        $loginArg = $args[$i + 1]
    }
}

if ($env:HEPHAESTOS_TEST_LOGIN_LOG) {
    $loginArg | Set-Content -LiteralPath $env:HEPHAESTOS_TEST_LOGIN_LOG
}

"Redirecting stderr to 'stub_stderr.log'"
"[  0%] Checking for available update..."
"Success. Downloaded item $workshopId to steamapps/workshop/content/$workshopAppid/$workshopId"

if ($env:HEPHAESTOS_TEST_AUTH_FAIL) {
    "Logging in user '$loginArg' to Steam Public...FAILED (Invalid Password)"
    exit 5
}

if (-not $env:HEPHAESTOS_TEST_WORKSHOP_NO_DOWNLOAD) {
    $downloaded = Join-Path $env:HEPHAESTOS_TEST_STEAM_ROOT "steamapps\workshop\content\$workshopAppid\$workshopId"
    New-Item -ItemType Directory -Path $downloaded -Force | Out-Null
    "fake mod content" | Set-Content -LiteralPath (Join-Path $downloaded "Info.json")
}

exit 0
'@ | Set-Content -LiteralPath $script:stubPathInstall -Encoding UTF8
    }

    BeforeEach {
        $script:steamRootInstall = Join-Path $TestDrive ([guid]::NewGuid().ToString())
        $script:manifestDirInstall = Join-Path $script:steamRootInstall "steamapps"
        New-Item -ItemType Directory -Path $script:manifestDirInstall -Force | Out-Null
        @'
"AppState"
{
	"appid"		"2394010"
	"installdir"		"PalServer"
	"buildid"		"100"
}
'@ | Set-Content -LiteralPath (Join-Path $script:manifestDirInstall "appmanifest_2394010.acf") -Encoding UTF8

        $env:HEPHAESTOS_TEST_STEAM_ROOT = $script:steamRootInstall
        $script:loginLogPath = Join-Path $TestDrive "login-$([guid]::NewGuid()).log"
        $env:HEPHAESTOS_TEST_LOGIN_LOG = $script:loginLogPath
        Remove-Item Env:\HEPHAESTOS_TEST_WORKSHOP_NO_DOWNLOAD -ErrorAction SilentlyContinue

        $script:cfgInstall = [pscustomobject]@{ steamcmd_root = $script:steamRootInstall; steamcmd = $script:stubPathInstall }
        $script:serverCfgInstall = [pscustomobject]@{ appid = 2394010; workshop_appid = 1623730 }
    }

    It "copie le dossier telecharge par steamcmd vers Mods/Workshop/<id> et ne leve pas d'exception" {
        { Install-WorkshopMod -Cfg $script:cfgInstall -ServerCfg $script:serverCfgInstall -WorkshopId "3147025543" } | Should -Not -Throw

        $destination = Join-Path $script:steamRootInstall "steamapps\common\PalServer\Mods\Workshop\3147025543\Info.json"
        Test-Path -LiteralPath $destination | Should -Be $true
    }

    It "utilise +login anonymous par defaut quand Cfg.steamcmd_login n'est pas defini" {
        Install-WorkshopMod -Cfg $script:cfgInstall -ServerCfg $script:serverCfgInstall -WorkshopId "3147025543"

        Get-Content -LiteralPath $script:loginLogPath | Should -Be "anonymous"
    }

    It "utilise le compte reel de Cfg.steamcmd_login s'il est defini (le Workshop de certains jeux refuse le login anonyme)" {
        $cfgWithLogin = [pscustomobject]@{
            steamcmd_root = $script:steamRootInstall
            steamcmd      = $script:stubPathInstall
            steamcmd_login = "aragorn467"
        }

        Install-WorkshopMod -Cfg $cfgWithLogin -ServerCfg $script:serverCfgInstall -WorkshopId "3147025543"

        Get-Content -LiteralPath $script:loginLogPath | Should -Be "aragorn467"
    }

    It "reinstalle un mod deja present sans imbriquer le dossier (destination deja existante)" {
        # Regression : Copy-Item vers un dossier destination deja existant copie la source
        # A L'INTERIEUR de la cible (<id>\<id>\...) au lieu de la remplacer.
        Install-WorkshopMod -Cfg $script:cfgInstall -ServerCfg $script:serverCfgInstall -WorkshopId "3147025543"

        { Install-WorkshopMod -Cfg $script:cfgInstall -ServerCfg $script:serverCfgInstall -WorkshopId "3147025543" } | Should -Not -Throw

        $destinationDir = Join-Path $script:steamRootInstall "steamapps\common\PalServer\Mods\Workshop\3147025543"
        $nestedDir = Join-Path $destinationDir "3147025543"
        Test-Path -LiteralPath $nestedDir | Should -Be $false
        Test-Path -LiteralPath (Join-Path $destinationDir "Info.json") | Should -Be $true
    }

    It "echec d'authentification steamcmd : message explicite demandant une reconnexion interactive" {
        # Le cache de credentials steamcmd (compte reel, Steam Guard) peut expirer : le
        # symptome brut est un tail de sortie cryptique. On veut un message actionnable.
        $env:HEPHAESTOS_TEST_AUTH_FAIL = "1"
        $cfgWithLogin = [pscustomobject]@{
            steamcmd_root = $script:steamRootInstall
            steamcmd      = $script:stubPathInstall
            steamcmd_login = "aragorn467"
        }

        { Install-WorkshopMod -Cfg $cfgWithLogin -ServerCfg $script:serverCfgInstall -WorkshopId "3147025543" } |
            Should -Throw "*authentification steamcmd*"

        Remove-Item Env:\HEPHAESTOS_TEST_AUTH_FAIL -ErrorAction SilentlyContinue
    }

    It "leve une exception explicite si steamcmd ne produit pas le dossier attendu" {
        $env:HEPHAESTOS_TEST_WORKSHOP_NO_DOWNLOAD = "1"

        { Install-WorkshopMod -Cfg $script:cfgInstall -ServerCfg $script:serverCfgInstall -WorkshopId "000000" } | Should -Throw
    }
}

Describe "Remove-WorkshopMod" {
    BeforeAll {
        $script:steamRootRemove = Join-Path $TestDrive "steam-remove"
        $script:manifestDirRemove = Join-Path $script:steamRootRemove "steamapps"
        New-Item -ItemType Directory -Path $script:manifestDirRemove -Force | Out-Null
        @'
"AppState"
{
	"appid"		"2394010"
	"installdir"		"PalServer"
	"buildid"		"100"
}
'@ | Set-Content -LiteralPath (Join-Path $script:manifestDirRemove "appmanifest_2394010.acf") -Encoding UTF8

        $script:workshopDirRemove = Join-Path $script:steamRootRemove "steamapps\common\PalServer\Mods\Workshop"
        $script:cfgRemove = [pscustomobject]@{ steamcmd_root = $script:steamRootRemove }
        $script:serverCfgRemove = [pscustomobject]@{ appid = 2394010 }
    }

    It "supprime le dossier du mod s'il existe" {
        $modDir = Join-Path $script:workshopDirRemove "3147025543"
        New-Item -ItemType Directory -Path $modDir -Force | Out-Null

        Remove-WorkshopMod -Cfg $script:cfgRemove -ServerCfg $script:serverCfgRemove -WorkshopId "3147025543"

        Test-Path -LiteralPath $modDir | Should -Be $false
    }

    It "ne leve pas d'exception si le mod est deja absent" {
        { Remove-WorkshopMod -Cfg $script:cfgRemove -ServerCfg $script:serverCfgRemove -WorkshopId "deja-parti" } | Should -Not -Throw
    }
}
