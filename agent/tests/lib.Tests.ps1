Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot/../hephaestos-lib.ps1"

    # Get-CimInstance (module CimCmdlets) est absent du conteneur Pester Linux -- meme
    # piege que Get-ScheduledTask (auto-discovery.Tests.ps1) : sans stub, "Mock
    # Get-CimInstance" leve CommandNotFoundException avant meme d'entrer dans la
    # fonction testee. Stub test-only, jamais utilise en code de production.
    if (-not (Get-Command Get-CimInstance -ErrorAction SilentlyContinue)) {
        function Get-CimInstance { param($ClassName, $Filter) }
    }

    # Write-HephLog vit dans hephaestos-agent.ps1 (pas hephaestos-lib.ps1, jamais dot-source ici) --
    # necessaire pour que Update-HephServersFromBackend puisse l'appeler sans
    # CommandNotFoundException. Stub test-only, jamais utilise en code de production.
    if (-not (Get-Command Write-HephLog -ErrorAction SilentlyContinue)) {
        function Write-HephLog { param($LogPath, $Message) }
    }
}

Describe "Get-LocalBuildId" {
    It "extrait le buildid d'un .acf valide" {
        $acf = Join-Path $TestDrive "appmanifest_2394010.acf"
        @'
"AppState"
{
	"appid"		"2394010"
	"Universe"		"1"
	"name"		"PalServer"
	"buildid"		"24088465"
}
'@ | Set-Content -LiteralPath $acf -Encoding UTF8

        Get-LocalBuildId -ManifestPath $acf | Should -Be "24088465"
    }

    It "leve une exception si le manifest est introuvable" {
        $missing = Join-Path $TestDrive "does-not-exist.acf"
        { Get-LocalBuildId -ManifestPath $missing } | Should -Throw
    }
}

Describe "Get-ProcessMetrics" {
    It "retourne CpuPercent et MemMb null si le process est introuvable" {
        $result = Get-ProcessMetrics -ProcessName "ProcessusInexistant12345"

        $result.CpuPercent | Should -Be $null
        $result.MemMb | Should -Be $null
    }

    It "retourne une valeur MemMb numerique pour un process reellement en cours (pwsh lui-meme)" {
        $selfName = (Get-Process -Id $PID).ProcessName

        $result = Get-ProcessMetrics -ProcessName $selfName

        $result.MemMb | Should -BeGreaterThan 0
    }

    It "normalise le CPU par le nombre de coeurs logiques (156% brut sur 6 coeurs -> 26%)" {
        Mock Get-CimInstance {
            [pscustomobject]@{ PercentProcessorTime = 156 }
        } -ParameterFilter { $ClassName -eq "Win32_PerfFormattedData_PerfProc_Process" }
        Mock Get-CimInstance {
            [pscustomobject]@{ NumberOfLogicalProcessors = 6 }
        } -ParameterFilter { $ClassName -eq "Win32_ComputerSystem" }
        $selfName = (Get-Process -Id $PID).ProcessName

        $result = Get-ProcessMetrics -ProcessName $selfName

        $result.CpuPercent | Should -Be 26.0
    }
}

Describe "Get-HephConfig" {
    It "charge une config JSON valide" {
        $cfgPath = Join-Path $TestDrive "hephaestos-config.json"
        @'
{
  "api_base": "http://127.0.0.1:8710",
  "agent_token": "abc123",
  "servers": [{"name": "palworld", "appid": 2394010}]
}
'@ | Set-Content -LiteralPath $cfgPath -Encoding UTF8

        $cfg = Get-HephConfig -Path $cfgPath
        $cfg.api_base | Should -Be "http://127.0.0.1:8710"
        $cfg.agent_token | Should -Be "abc123"
        $cfg.servers.Count | Should -Be 1
    }

    It "leve une exception explicite si api_base est present mais vide (throw du code, pas StrictMode)" {
        # Cle PRESENTE a null, pas absente : sous Set-StrictMode -Version Latest, un acces a une
        # cle ABSENTE leve deja PropertyNotFoundException avant d'atteindre le throw explicite du
        # code (verifie par isolation le 13/07 : $cfg.api_base sur cle absente => PropertyNotFoundException,
        # sur cle presente a $null => pas d'exception, le code doit faire le travail). Ce cas-ci
        # passe forcement par le "if (-not $cfg.api_base) { throw ... }" de Get-HephConfig.
        $cfgPath = Join-Path $TestDrive "hephaestos-config-bad.json"
        @'
{
  "api_base": null,
  "agent_token": "abc123",
  "servers": []
}
'@ | Set-Content -LiteralPath $cfgPath -Encoding UTF8

        { Get-HephConfig -Path $cfgPath } | Should -Throw -ExpectedMessage "*Config invalide*api_base*"
    }

    It "leve une exception explicite si agent_token est present mais vide" {
        $cfgPath = Join-Path $TestDrive "hephaestos-config-bad-token.json"
        @'
{
  "api_base": "http://127.0.0.1:8710",
  "agent_token": null,
  "servers": []
}
'@ | Set-Content -LiteralPath $cfgPath -Encoding UTF8

        { Get-HephConfig -Path $cfgPath } | Should -Throw -ExpectedMessage "*Config invalide*agent_token*"
    }

    It "leve une exception explicite si servers est present mais null" {
        $cfgPath = Join-Path $TestDrive "hephaestos-config-bad-servers.json"
        @'
{
  "api_base": "http://127.0.0.1:8710",
  "agent_token": "abc123",
  "servers": null
}
'@ | Set-Content -LiteralPath $cfgPath -Encoding UTF8

        { Get-HephConfig -Path $cfgPath } | Should -Throw -ExpectedMessage "*Config invalide*servers*"
    }

    It "leve une exception si le fichier est introuvable" {
        { Get-HephConfig -Path (Join-Path $TestDrive "nope.json") } | Should -Throw
    }
}

Describe "Send-KumaPush" {
    It "construit l'URI avec ?status= et msg encode (pieges PS du 13/07)" {
        Mock Invoke-RestMethod { return @{ ok = $true } }

        Send-KumaPush -PushUrl "https://uptime-kuma.example.com/api/push/abc" -Status "up" -Msg "A jour"

        Should -Invoke Invoke-RestMethod -Times 1 -ParameterFilter {
            $Uri -eq "https://uptime-kuma.example.com/api/push/abc?status=up&msg=A%20jour"
        }
    }
}

Describe "Get-ManifestPath" {
    It "construit le chemin conventionnel steamapps/appmanifest_<appid>.acf" {
        # NOTE : SteamRoot utilise $TestDrive (pas un litteral "C:\steam") -- Join-Path sur
        # PowerShell Linux (conteneur de test) tente de resoudre un PSDrive nomme d'apres la
        # lettre et leve "Cannot find drive" pour TOUT chemin "C:\..." litteral, meme sans
        # jamais toucher le disque. Verifie par repro directe le 14/07. $TestDrive est un vrai
        # repertoire (Linux ou Windows), donc reste discriminant sans ce piege d'environnement.
        $steamRoot = Join-Path $TestDrive "steam"
        Get-ManifestPath -SteamRoot $steamRoot -AppId 4129620 | Should -Be (Join-Path $steamRoot "steamapps\appmanifest_4129620.acf")
    }
}

Describe "Get-InstallDirFromManifest" {
    It "extrait installdir d'un manifest valide (avec espace dans le nom)" {
        $acf = Join-Path $TestDrive "appmanifest_4129620.acf"
        @'
"AppState"
{
	"appid"		"4129620"
	"installdir"		"Windrose Dedicated Server"
	"buildid"		"23276652"
}
'@ | Set-Content -LiteralPath $acf -Encoding UTF8

        Get-InstallDirFromManifest -ManifestPath $acf | Should -Be "Windrose Dedicated Server"
    }

    It "leve une exception si le manifest est introuvable" {
        $missing = Join-Path $TestDrive "does-not-exist.acf"
        { Get-InstallDirFromManifest -ManifestPath $missing } | Should -Throw
    }

    It "leve une exception si installdir est absent du manifest" {
        $acf = Join-Path $TestDrive "appmanifest_no_installdir.acf"
        @'
"AppState"
{
	"appid"		"4129620"
	"buildid"		"23276652"
}
'@ | Set-Content -LiteralPath $acf -Encoding UTF8

        { Get-InstallDirFromManifest -ManifestPath $acf } | Should -Throw
    }
}

Describe "Get-ServerInstallDir" {
    It "combine steamcmd_root + installdir du manifest pour produire le vrai chemin d'installation" {
        $steamRoot = Join-Path $TestDrive "steam"
        $manifestDir = Join-Path $steamRoot "steamapps"
        New-Item -ItemType Directory -Path $manifestDir -Force | Out-Null
        @'
"AppState"
{
	"appid"		"4129620"
	"installdir"		"Windrose Dedicated Server"
	"buildid"		"23276652"
}
'@ | Set-Content -LiteralPath (Join-Path $manifestDir "appmanifest_4129620.acf") -Encoding UTF8

        $result = Get-ServerInstallDir -SteamRoot $steamRoot -AppId 4129620
        $result | Should -Be (Join-Path $steamRoot "steamapps\common\Windrose Dedicated Server")
    }
}

Describe "Invoke-HephApi" {
    It "envoie le header Authorization Bearer avec le token agent" {
        Mock Invoke-RestMethod { return @{ orders = @() } }

        $cfg = [pscustomobject]@{
            api_base    = "http://127.0.0.1:8710"
            agent_token = "supersecret"
        }

        Invoke-HephApi -Cfg $cfg -Method "GET" -Path "/api/agent/orders" | Out-Null

        Should -Invoke Invoke-RestMethod -Times 1 -ParameterFilter {
            $Headers["Authorization"] -eq "Bearer supersecret"
        }
    }
}

Describe "Update-HephServersFromBackend" {
    BeforeEach {
        $script:cfgPath = Join-Path $TestDrive "hephaestos-config.json"
        @{ api_base = "http://x"; agent_token = "t"; steamcmd_root = "C:\steam"
           servers = @(@{ name = "palworld"; appid = 2394010 }) } |
            ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $script:cfgPath
        $script:cfg = Get-HephConfig -Path $script:cfgPath
    }

    It "applique et persiste quand le hash differe, en preservant les cles locales" {
        $backend = [pscustomobject]@{
            hash = "h1"
            servers = @([pscustomobject]@{ name = "palworld"; appid = 2394010; launch_args = "-x" })
        }
        $out = Update-HephServersFromBackend -Cfg $script:cfg -ConfigPath $script:cfgPath -BackendConfig $backend
        $out.backend_config_hash | Should -Be "h1"
        $out.servers[0].launch_args | Should -Be "-x"
        $reloaded = Get-HephConfig -Path $script:cfgPath
        $reloaded.agent_token | Should -Be "t"          # cle locale intacte
        $reloaded.backend_config_hash | Should -Be "h1"  # persiste
    }

    It "no-op si hash identique ou bloc absent" {
        $before = (Get-Item -LiteralPath $script:cfgPath).LastWriteTimeUtc
        $null = Update-HephServersFromBackend -Cfg $script:cfg -ConfigPath $script:cfgPath -BackendConfig $null
        $same = [pscustomobject]@{ hash = "h2"; servers = @() }
        $script:cfg | Add-Member -NotePropertyName backend_config_hash -NotePropertyValue "h2"
        $null = Update-HephServersFromBackend -Cfg $script:cfg -ConfigPath $script:cfgPath -BackendConfig $same
        (Get-Item -LiteralPath $script:cfgPath).LastWriteTimeUtc | Should -Be $before
    }

    It "apres une reecriture reussie, aucun fichier temporaire ne traine a cote de la config" {
        # Portage du test d'ecriture atomique disparu avec Invoke-GameAutoDiscovery --
        # Update-HephServersFromBackend fait le meme tmp+rename sur le meme fichier
        # (backend_config_hash absent -> agent qui ne redemarre plus, Get-HephConfig
        # throw sur du JSON invalide).
        $backend = [pscustomobject]@{
            hash    = "h3"
            servers = @([pscustomobject]@{ name = "palworld"; appid = 2394010 })
        }

        Update-HephServersFromBackend -Cfg $script:cfg -ConfigPath $script:cfgPath -BackendConfig $backend | Out-Null

        Test-Path -LiteralPath "$($script:cfgPath).tmp" | Should -Be $false
        { Get-HephConfig -Path $script:cfgPath } | Should -Not -Throw
    }

    It "un crash en pleine ecriture ne corrompt pas la config existante (write-tmp puis rename)" {
        # Injection de panne : Set-Content ecrit un contenu tronque puis crashe, comme un
        # disque plein / kill en pleine ecriture. La corruption atterrit sur le fichier
        # .tmp (seul argument passe a Set-Content par la fonction) -- Move-Item n'est
        # jamais atteint, donc ConfigPath (le vrai fichier) doit rester intact.
        $originalContent = Get-Content -LiteralPath $script:cfgPath -Raw
        Mock Set-Content {
            [IO.File]::WriteAllText($LiteralPath, "{TRONQUE")
            throw "crash simule en pleine ecriture"
        }
        $backend = [pscustomobject]@{
            hash    = "h4"
            servers = @([pscustomobject]@{ name = "palworld"; appid = 2394010 })
        }

        { Update-HephServersFromBackend -Cfg $script:cfg -ConfigPath $script:cfgPath -BackendConfig $backend } | Should -Throw

        # la config d'origine est intacte et toujours parsable
        (Get-Content -LiteralPath $script:cfgPath -Raw).Trim() | Should -Be $originalContent.Trim()
        { Get-HephConfig -Path $script:cfgPath } | Should -Not -Throw
    }
}
