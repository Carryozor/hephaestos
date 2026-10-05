Set-StrictMode -Version Latest

# Durcissement de la lib (v2.4.0) : P3 (timeouts), P4 (redirections BepInEx manuelles),
# P9 (parsing ShowPlayers), P10 (validations), P12 (rafraichissement du verrou).

BeforeAll {
    . "$PSScriptRoot/../hephaestos-lib.ps1"
    . "$PSScriptRoot/../hephaestos-agent.ps1"

    # Exception de redirection 3xx : MaximumRedirection 0 fait lever Invoke-WebRequest
    # avec la reponse 3xx attachee (5.1 : WebException/HttpWebResponse, 7 : HttpResponseException).
    function New-RedirectError {
        param([int]$Code = 302, $Location = $null, [switch]$Ps7Style)
        $ex = [System.Exception]::new("redirection ${Code}")
        if ($Ps7Style) {
            $locValue = $null
            if ($Location) { $locValue = [Uri]$Location }
            $headers = [pscustomobject]@{ Location = $locValue }
        } else {
            $headers = @{}
            if ($Location) { $headers["Location"] = $Location }
        }
        Add-Member -InputObject $ex -NotePropertyName Response -NotePropertyValue ([pscustomobject]@{
            StatusCode = [System.Net.HttpStatusCode]$Code
            Headers    = $headers
        })
        return $ex
    }
}

Describe "P3 -- timeouts des appels HTTP" {
    It "Get-PublicBuildId passe un TimeoutSec fini a Invoke-RestMethod" {
        Mock Invoke-RestMethod {
            [pscustomobject]@{ data = [pscustomobject]@{ "123" = [pscustomobject]@{ depots = [pscustomobject]@{ branches = [pscustomobject]@{ public = [pscustomobject]@{ buildid = "9" } } } } } }
        }
        Get-PublicBuildId -AppId 123 | Should -Be "9"
        Should -Invoke Invoke-RestMethod -Times 1 -Exactly -ParameterFilter { $TimeoutSec -gt 0 -and $TimeoutSec -le 60 }
    }

    It "Send-KumaPush passe un TimeoutSec fini a Invoke-RestMethod" {
        Mock Invoke-RestMethod { [pscustomobject]@{ ok = $true } }
        Send-KumaPush -PushUrl "https://kuma/api/push/tok" -Status "up" | Out-Null
        Should -Invoke Invoke-RestMethod -Times 1 -Exactly -ParameterFilter { $TimeoutSec -gt 0 -and $TimeoutSec -le 60 }
    }

    It "Invoke-BepInExDownload passe un TimeoutSec plus large (telechargement) a Invoke-WebRequest" {
        $dest = Join-Path $TestDrive "timeout.zip"
        Mock Invoke-WebRequest { "x" | Set-Content -LiteralPath $dest; $null }
        Invoke-BepInExDownload -Url "https://thunderstore.io/x.zip" -DestPath $dest
        Should -Invoke Invoke-WebRequest -Times 1 -Exactly -ParameterFilter { $TimeoutSec -ge 60 -and $TimeoutSec -le 900 }
    }
}

Describe "P4 -- Invoke-BepInExDownload : redirections suivies a la main, fail-closed" {
    BeforeEach {
        $script:dest = Join-Path $TestDrive "dl-$(Get-Random).zip"
        $script:requested = @()
        $script:routes = @{}
        $script:okBody = { "contenu" | Set-Content -LiteralPath $script:dest; $null }
        Mock Invoke-WebRequest {
            $uriText = [string]$Uri
            $script:requested += $uriText
            if (-not $script:routes.ContainsKey($uriText)) { throw "route de test inattendue: ${uriText}" }
            $route = $script:routes[$uriText]
            if ($route -is [string]) { throw (New-RedirectError -Code 302 -Location $route) }
            & $route
        }
    }

    It "n'utilise jamais le suivi automatique : -MaximumRedirection 0 a chaque requete" {
        $script:routes["https://thunderstore.io/x.zip"] = $script:okBody
        Invoke-BepInExDownload -Url "https://thunderstore.io/x.zip" -DestPath $script:dest
        Should -Invoke Invoke-WebRequest -Times 1 -Exactly -ParameterFilter { $MaximumRedirection -eq 0 }
    }

    It "suit une redirection vers un hote de l'allowlist (style 5.1, en-tetes table) et telecharge" {
        $script:routes["https://thunderstore.io/x.zip"] = { throw (New-RedirectError -Code 302 -Location "https://gcdn.thunderstore.io/y.zip") }
        $script:routes["https://gcdn.thunderstore.io/y.zip"] = $script:okBody
        Invoke-BepInExDownload -Url "https://thunderstore.io/x.zip" -DestPath $script:dest
        $script:requested | Should -Be @("https://thunderstore.io/x.zip", "https://gcdn.thunderstore.io/y.zip")
        Test-Path -LiteralPath $script:dest | Should -BeTrue
    }

    It "suit une redirection (style PowerShell 7, Headers.Location de type Uri)" {
        $script:routes["https://thunderstore.io/x.zip"] = { throw (New-RedirectError -Code 301 -Location "https://github.com/a/b.zip" -Ps7Style) }
        $script:routes["https://github.com/a/b.zip"] = $script:okBody
        Invoke-BepInExDownload -Url "https://thunderstore.io/x.zip" -DestPath $script:dest
        $script:requested.Count | Should -Be 2
    }

    It "resout une Location relative contre l'URL courante" {
        $script:routes["https://thunderstore.io/x.zip"] = { throw (New-RedirectError -Code 302 -Location "/autre/y.zip") }
        $script:routes["https://thunderstore.io/autre/y.zip"] = $script:okBody
        Invoke-BepInExDownload -Url "https://thunderstore.io/x.zip" -DestPath $script:dest
        $script:requested[1] | Should -Be "https://thunderstore.io/autre/y.zip"
    }

    It "reponse 3xx non levee en exception (StatusCode + Headers sur l'objet retourne) : suivie aussi" {
        $script:routes["https://thunderstore.io/x.zip"] = {
            [pscustomobject]@{ StatusCode = 302; Headers = @{ Location = "https://gcdn.thunderstore.io/y.zip" } }
        }
        $script:routes["https://gcdn.thunderstore.io/y.zip"] = $script:okBody
        Invoke-BepInExDownload -Url "https://thunderstore.io/x.zip" -DestPath $script:dest
        $script:requested.Count | Should -Be 2
    }

    It "redirection vers un hote HORS allowlist : echec et AUCUNE requete emise vers lui" {
        $script:routes["https://thunderstore.io/x.zip"] = { throw (New-RedirectError -Code 302 -Location "https://evil.example.com/payload.zip") }
        $script:routes["https://evil.example.com/payload.zip"] = $script:okBody
        { Invoke-BepInExDownload -Url "https://thunderstore.io/x.zip" -DestPath $script:dest } | Should -Throw "*non autorise*"
        Should -Invoke Invoke-WebRequest -Times 0 -Exactly -ParameterFilter { $Uri -like "*evil.example.com*" }
        Should -Invoke Invoke-WebRequest -Times 1 -Exactly
        Test-Path -LiteralPath $script:dest | Should -BeFalse
    }

    It "saut vers http:// (meme hote autorise) : refuse sans requete" {
        $script:routes["https://thunderstore.io/x.zip"] = { throw (New-RedirectError -Code 302 -Location "http://thunderstore.io/y.zip") }
        $script:routes["http://thunderstore.io/y.zip"] = $script:okBody
        { Invoke-BepInExDownload -Url "https://thunderstore.io/x.zip" -DestPath $script:dest } | Should -Throw "*https*"
        Should -Invoke Invoke-WebRequest -Times 0 -Exactly -ParameterFilter { $Uri -like "http://*" }
    }

    It "boucle de redirections (plus de 5 sauts) : echec, nombre de requetes borne" {
        $script:routes["https://thunderstore.io/loop.zip"] = { throw (New-RedirectError -Code 302 -Location "https://thunderstore.io/loop.zip") }
        { Invoke-BepInExDownload -Url "https://thunderstore.io/loop.zip" -DestPath $script:dest } | Should -Throw "*trop de redirections*"
        $script:requested.Count | Should -BeLessOrEqual 6
        Test-Path -LiteralPath $script:dest | Should -BeFalse
    }

    It "5 sauts exactement : accepte (la limite est 5, pas 4)" {
        for ($i = 0; $i -lt 5; $i++) {
            $script:routes["https://thunderstore.io/h$i.zip"] = "https://thunderstore.io/h$($i + 1).zip"
        }
        $script:routes["https://thunderstore.io/h5.zip"] = $script:okBody
        { Invoke-BepInExDownload -Url "https://thunderstore.io/h0.zip" -DestPath $script:dest } | Should -Not -Throw
        $script:requested.Count | Should -Be 6
    }

    It "URL finale indeterminee (3xx sans en-tete Location) : echec, fail-closed" {
        $script:routes["https://thunderstore.io/x.zip"] = { throw (New-RedirectError -Code 302) }
        { Invoke-BepInExDownload -Url "https://thunderstore.io/x.zip" -DestPath $script:dest } | Should -Throw "*Location*"
        Test-Path -LiteralPath $script:dest | Should -BeFalse
    }

    It "URL finale indeterminee (Location inexploitable) : echec" {
        $script:routes["https://thunderstore.io/x.zip"] = { throw (New-RedirectError -Code 302 -Location "http://[::bad") }
        { Invoke-BepInExDownload -Url "https://thunderstore.io/x.zip" -DestPath $script:dest } | Should -Throw
        Should -Invoke Invoke-WebRequest -Times 1 -Exactly
    }

    It "une erreur HTTP non-redirection (404) reste un echec de telechargement" {
        $script:routes["https://thunderstore.io/x.zip"] = {
            $ex = [System.Exception]::new("404")
            Add-Member -InputObject $ex -NotePropertyName Response -NotePropertyValue ([pscustomobject]@{ StatusCode = [System.Net.HttpStatusCode]404; Headers = @{} })
            throw $ex
        }
        { Invoke-BepInExDownload -Url "https://thunderstore.io/x.zip" -DestPath $script:dest } | Should -Throw "*echec telechargement*"
    }

    It "la reponse finale annonce une URI hors allowlist : echec et fichier supprime (garde-fou conserve)" {
        $script:routes["https://thunderstore.io/x.zip"] = {
            "contenu" | Set-Content -LiteralPath $script:dest
            [pscustomobject]@{ BaseResponse = [pscustomobject]@{ ResponseUri = [Uri]"https://evil.example.com/x.zip" } }
        }
        { Invoke-BepInExDownload -Url "https://thunderstore.io/x.zip" -DestPath $script:dest } | Should -Throw
        Test-Path -LiteralPath $script:dest | Should -BeFalse
    }
}

Describe "P9 -- Get-PalworldPlayers : parsing ShowPlayers depuis la fin de ligne" {
    BeforeEach {
        $script:fakeInstall = Join-Path $TestDrive "fake-install"
        Mock Get-ServerInstallDir { $script:fakeInstall }
        Mock Get-PalworldAdminPassword { "pwd" }
        $script:cfg = [pscustomobject]@{ steamcmd_root = "C:\steam" }
        $script:srv = [pscustomobject]@{ name = "palworld"; appid = 2394010; rcon = [pscustomobject]@{ host = "127.0.0.1"; port = 25575 } }
    }

    It "nom contenant une virgule : 'Bob, le Brave' reste entier, playeruid et steamid corrects" {
        Mock Invoke-Rcon { "name,playeruid,steamid`nBob, le Brave,1234567890,76561198000000001`nAlice,AAA111,76561198000000002" }
        $r = Get-PalworldPlayers -Cfg $script:cfg -ServerCfg $script:srv
        $r.Count | Should -Be 2
        $r.Players[0].name | Should -Be "Bob, le Brave"
        $r.Players[0].playeruid | Should -Be "1234567890"
        $r.Players[0].steamid | Should -Be "76561198000000001"
        $r.Players[1].name | Should -Be "Alice"
    }

    It "plusieurs virgules dans le nom" {
        Mock Invoke-Rcon { "name,playeruid,steamid`na,b,c,UID9,STEAM9" }
        $r = Get-PalworldPlayers -Cfg $script:cfg -ServerCfg $script:srv
        $r.Players[0].name | Should -Be "a,b,c"
        $r.Players[0].playeruid | Should -Be "UID9"
        $r.Players[0].steamid | Should -Be "STEAM9"
    }

    It "fins de ligne CRLF : pas de retour chariot residuel dans le steamid" {
        Mock Invoke-Rcon { "name,playeruid,steamid`r`nAlice,AAA111,76561198000000002`r`n" }
        $r = Get-PalworldPlayers -Cfg $script:cfg -ServerCfg $script:srv
        $r.Players[0].steamid | Should -Be "76561198000000002"
    }

    It "ligne a moins de 3 champs ignoree, en-tete seul = 0 joueur" {
        Mock Invoke-Rcon { "name,playeruid,steamid`nbizarre,seulement-deux" }
        $r = Get-PalworldPlayers -Cfg $script:cfg -ServerCfg $script:srv
        $r.Count | Should -Be 0
    }
}

Describe "P10a -- Get-GameSaveDir : save_dir confine a l'install" {
    BeforeEach {
        $script:root = Join-Path $TestDrive "gsd-$(Get-Random)"
        New-Item -ItemType Directory -Path (Join-Path $script:root "steamapps") -Force | Out-Null
        @'
"AppState"
{
	"appid"		"2394010"
	"installdir"		"PalServer"
	"buildid"		"100"
}
'@ | Set-Content -LiteralPath (Join-Path $script:root "steamapps\appmanifest_2394010.acf") -Encoding UTF8
        $script:cfg = [pscustomobject]@{ steamcmd_root = $script:root }
    }

    It "save_dir relatif valide : chemin sous l'install" {
        $srv = [pscustomobject]@{ name = "p"; appid = 2394010; save_dir = "Pal\Saved\SaveGames" }
        Get-GameSaveDir -Cfg $script:cfg -ServerCfg $srv | Should -BeLike "*PalServer*Pal*SaveGames"
    }

    It "save_dir absent ou vide : `$null (backups desactives), inchange" {
        Get-GameSaveDir -Cfg $script:cfg -ServerCfg ([pscustomobject]@{ name = "p"; appid = 2394010 }) | Should -BeNullOrEmpty
        Get-GameSaveDir -Cfg $script:cfg -ServerCfg ([pscustomobject]@{ name = "p"; appid = 2394010; save_dir = "" }) | Should -BeNullOrEmpty
    }

    It "refuse '<value>'" -ForEach @(
        @{ value = '..\..\Windows' },
        @{ value = 'Pal/../../x' },
        @{ value = 'Pal\..' },
        @{ value = 'C:\Windows\System32' },
        @{ value = 'D:save' },
        @{ value = '\\srv\share\saves' },
        @{ value = '/etc/passwd' },
        @{ value = '\Windows' }
    ) {
        $srv = [pscustomobject]@{ name = "p"; appid = 2394010; save_dir = $value }
        { Get-GameSaveDir -Cfg $script:cfg -ServerCfg $srv } | Should -Throw "*save_dir*"
    }

    It "T2.8 refuse un save_dir qui se resout sur la racine d'install : '<value>'" -ForEach @(
        @{ value = './' },
        @{ value = '.\' },
        @{ value = '.' },
        @{ value = 'Saves\..' },
        @{ value = 'x\..\' }
    ) {
        $srv = [pscustomobject]@{ name = "p"; appid = 2394010; save_dir = $value }
        { Get-GameSaveDir -Cfg $script:cfg -ServerCfg $srv } | Should -Throw "*save_dir*"
    }

    It "T2.8 accepte '<value>' (strictement sous la racine)" -ForEach @(
        @{ value = 'Pal\Saved\SaveGames' },
        @{ value = 'Saved\SaveGames' }
    ) {
        $srv = [pscustomobject]@{ name = "p"; appid = 2394010; save_dir = $value }
        { Get-GameSaveDir -Cfg $script:cfg -ServerCfg $srv } | Should -Not -Throw
    }

    It "accepte un nom contenant des points non traversants ('..backup' n'est pas '..')" {
        $srv = [pscustomobject]@{ name = "p"; appid = 2394010; save_dir = "Saves\v1..2" }
        { Get-GameSaveDir -Cfg $script:cfg -ServerCfg $srv } | Should -Not -Throw
    }
}

Describe "P10b -- Test-HephSafeName / start_task et process issus du backend" {
    It "accepte '<value>'" -ForEach @(
        @{ value = "PalServer" },
        @{ value = "PalServer-Win64-Shipping-Cmd" },
        @{ value = "Windrose Dedicated Server" },
        @{ value = "valheim_server" },
        @{ value = "Dossier\Tache_1.0" },
        @{ value = "Serveur Ete" }
    ) {
        Test-HephSafeName -Value $value | Should -BeTrue
    }

    It "refuse '<name>'" -ForEach @(
        @{ name = "etoile"; value = "*" },
        @{ name = "joker ?"; value = "Pal?erver" },
        @{ name = "guillemet double"; value = 'a"b' },
        @{ name = "apostrophe"; value = "a'b" },
        @{ name = "point-virgule"; value = "a;b" },
        @{ name = "et commercial"; value = "a&b" },
        @{ name = "pipe"; value = "a|b" },
        @{ name = "inferieur"; value = "a<b" },
        @{ name = "superieur"; value = "a>b" },
        @{ name = "backtick"; value = 'a`b' },
        @{ name = "dollar"; value = 'a$b' },
        @{ name = "saut de ligne"; value = "a`nb" },
        @{ name = "saut de ligne final"; value = "PalServer`n" },
        @{ name = "retour chariot"; value = "a`rb" },
        @{ name = "vide"; value = "" },
        @{ name = "null"; value = $null }
    ) {
        Test-HephSafeName -Value $value | Should -BeFalse
    }

    It "Start-GameServer refuse un start_task dangereux sans appeler schtasks" {
        Mock Invoke-Schtasks { 0 }
        $srv = [pscustomobject]@{ name = "x"; start_task = "T /F & calc"; process = "x" }
        { Start-GameServer -ServerCfg $srv } | Should -Throw "*start_task*"
        Should -Invoke Invoke-Schtasks -Times 0 -Exactly
    }

    It "Start-GameServer refuse un process a joker" {
        Mock Invoke-Schtasks { 0 }
        $srv = [pscustomobject]@{ name = "x"; start_task = "PalServer"; process = "*" }
        { Start-GameServer -ServerCfg $srv } | Should -Throw "*process*"
        Should -Invoke Invoke-Schtasks -Times 0 -Exactly
    }

    It "Stop-GameServer refuse un process a joker (sinon Get-Process -Name * + taskkill sur tout)" {
        Mock Get-Process { throw "ne doit pas etre appele" }
        Mock Invoke-Taskkill {}
        $srv = [pscustomobject]@{ name = "x"; process = "*"; stop_adapter = "generic-force" }
        { Stop-GameServer -Cfg ([pscustomobject]@{}) -ServerCfg $srv } | Should -Throw "*process*"
        Should -Invoke Invoke-Taskkill -Times 0 -Exactly
    }

    It "Update-HephServersFromBackend ignore (avec log) les serveurs a start_task/process dangereux, garde les autres" {
        $cfgPath = Join-Path $TestDrive "cfg-$(Get-Random).json"
        $log = Join-Path $TestDrive "cfg-$(Get-Random).log"
        $cfg = [pscustomobject]@{ api_base = "x"; agent_token = "t"; servers = @() }
        $backend = [pscustomobject]@{
            hash    = "h1"
            servers = @(
                [pscustomobject]@{ name = "bon"; appid = 1; process = "PalServer-Win64-Shipping-Cmd"; start_task = "PalServer" },
                [pscustomobject]@{ name = "mauvais-task"; appid = 2; process = "ok"; start_task = 'T"; calc' },
                [pscustomobject]@{ name = "mauvais-proc"; appid = 3; process = "*"; start_task = "T" }
            )
        }
        $result = Update-HephServersFromBackend -Cfg $cfg -ConfigPath $cfgPath -BackendConfig $backend -LogPath $log
        @($result.servers).Count | Should -Be 1
        $result.servers[0].name | Should -Be "bon"
        (Get-Content -LiteralPath $log -Raw) | Should -Match "mauvais-task"
        (Get-Content -LiteralPath $log -Raw) | Should -Match "mauvais-proc"
    }

    It "Invoke-SetupServer refuse un task_name dangereux (aucune tache creee)" {
        Mock New-GameStartTaskIfMissing {}
        $root = Join-Path $TestDrive "setup-$(Get-Random)"
        New-Item -ItemType Directory -Path (Join-Path $root "steamapps\common\Jeu") -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $root "steamapps\common\Jeu\g.exe") -Value "x"
        Set-Content -LiteralPath (Join-Path $root "steamapps\appmanifest_5.acf") -Value "`"AppState`"`n{`n`t`"appid`"`t`t`"5`"`n`t`"installdir`"`t`t`"Jeu`"`n}"
        $order = [pscustomobject]@{ appid = 5; exe_path = "g.exe"; task_name = 'x" /RU SYSTEM'; process = "g"; server = "s" }
        $r = Invoke-SetupServer -Cfg ([pscustomobject]@{ steamcmd_root = $root }) -Order $order
        $r.ok | Should -BeFalse
        $r.detail | Should -Match "task_name"
        Should -Invoke New-GameStartTaskIfMissing -Times 0 -Exactly
    }
}

Describe "P12 -- Start-HephSleepWithLock / Stop-GameServer rafraichit le verrou" {
    BeforeAll {
        $script:steamRootL = Join-Path $TestDrive "steam-lock"
        $manifestDir = Join-Path $script:steamRootL "steamapps"
        New-Item -ItemType Directory -Path $manifestDir -Force | Out-Null
        @'
"AppState"
{
	"appid"		"2394010"
	"installdir"		"PalServer"
	"buildid"		"100"
}
'@ | Set-Content -LiteralPath (Join-Path $manifestDir "appmanifest_2394010.acf") -Encoding UTF8
        $iniDir = Join-Path $script:steamRootL "steamapps\common\PalServer\Pal\Saved\Config\WindowsServer"
        New-Item -ItemType Directory -Path $iniDir -Force | Out-Null
        "OptionSettings=(AdminPassword=`"pwd123`")" | Set-Content -LiteralPath (Join-Path $iniDir "PalWorldSettings.ini") -Encoding UTF8
    }

    BeforeEach {
        $script:lock = Join-Path $TestDrive "agent-$(Get-Random).lock"
        Set-Content -LiteralPath $script:lock -Value "vieux"
        (Get-Item -LiteralPath $script:lock).LastWriteTime = (Get-Date).AddHours(-1)
        $script:sleeps = @()
        $script:lockAgesAtSleep = @()
        $script:down = $false
        Mock Start-Sleep {
            $script:sleeps += $Seconds
            $script:lockAgesAtSleep += ((Get-Date) - (Get-Item -LiteralPath $script:lock).LastWriteTime).TotalSeconds
            # on revieillit le verrou : chaque morceau doit etre rafraichi par SA PROPRE touche
            (Get-Item -LiteralPath $script:lock).LastWriteTime = (Get-Date).AddHours(-1)
        }
    }

    It "Start-HephSleepWithLock decoupe en morceaux de 60 s et touche le verrou avant chacun" {
        Start-HephSleepWithLock -Seconds 150 -LockPath $script:lock
        ($script:sleeps -join ",") | Should -Be "60,60,30"
        foreach ($age in $script:lockAgesAtSleep) { $age | Should -BeLessThan 30 }
    }

    It "Start-HephSleepWithLock sans LockPath : un seul Start-Sleep, aucun fichier cree" {
        Start-HephSleepWithLock -Seconds 150
        ($script:sleeps -join ",") | Should -Be "150"
    }

    It "Stop-GameServer palworld avec -LockPath : le decompte stop_warn_seconds rafraichit le verrou" {
        Mock Get-Process { if ($script:down) { $null } else { [pscustomobject]@{ Id = 4242 } } }
        Mock Get-PalworldPlayers { throw "comptage inconnu" }
        Mock Invoke-Rcon { if ($Command -like "Shutdown*") { $script:down = $true } }
        Mock Wait-Process {}
        Mock Invoke-Taskkill {}
        $cfg = [pscustomobject]@{ steamcmd_root = $script:steamRootL }
        $srv = [pscustomobject]@{
            name = "palworld"; appid = 2394010; process = "PalServer-Win64-Shipping-Cmd"
            stop_adapter = "palworld-rcon"; stop_warn_seconds = 150
            rcon = [pscustomobject]@{ host = "127.0.0.1"; port = 25575 }
        }

        Stop-GameServer -Cfg $cfg -ServerCfg $srv -LockPath $script:lock

        $warnChunks = @($script:sleeps | Where-Object { $_ -ne 10 })
        ($warnChunks -join ",") | Should -Be "60,60,30"
        $i = 0
        foreach ($age in $script:lockAgesAtSleep) {
            if ($script:sleeps[$i] -ne 10) { $age | Should -BeLessThan 30 }
            $i++
        }
    }

    It "Stop-GameServer palworld sans -LockPath : un seul Start-Sleep de 150 s (comportement historique)" {
        Mock Get-Process { if ($script:down) { $null } else { [pscustomobject]@{ Id = 4242 } } }
        Mock Get-PalworldPlayers { throw "comptage inconnu" }
        Mock Invoke-Rcon { if ($Command -like "Shutdown*") { $script:down = $true } }
        Mock Wait-Process {}
        Mock Invoke-Taskkill {}
        $cfg = [pscustomobject]@{ steamcmd_root = $script:steamRootL }
        $srv = [pscustomobject]@{
            name = "palworld"; appid = 2394010; process = "PalServer-Win64-Shipping-Cmd"
            stop_adapter = "palworld-rcon"; stop_warn_seconds = 150
            rcon = [pscustomobject]@{ host = "127.0.0.1"; port = 25575 }
        }
        Stop-GameServer -Cfg $cfg -ServerCfg $srv
        (@($script:sleeps | Where-Object { $_ -ne 10 }) -join ",") | Should -Be "150"
    }
}
