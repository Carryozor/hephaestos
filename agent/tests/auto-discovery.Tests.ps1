Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot/../hephaestos-lib.ps1"
    $script:realRegistryPath = "$PSScriptRoot/../known-games.json"

    # Get-ScheduledTask (module ScheduledTasks) n'existe pas sur le pwsh Linux du conteneur
    # de test -- contrairement a Get-Process, deja cross-platform et mocke ailleurs dans ce
    # repo. Pester ne peut pas mocker une commande totalement absente (CommandNotFoundException
    # levee par Mock lui-meme). Stub conditionnel ICI (jamais dans hephaestos-lib.ps1) : sur un vrai
    # Windows CI, Get-Command la trouve deja et ce stub n'est jamais defini, donc le vrai
    # cmdlet reste utilise sans etre masque.
    if (-not (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue)) {
        function Get-ScheduledTask { param($TaskName) }
    }

    # Definie ici (BeforeAll) et non au niveau du Describe : le code d'un Describe hors
    # bloc Before*/It ne s'execute qu'en phase Discovery chez Pester v5, pas en phase Run
    # -- une fonction ainsi declaree "au niveau du Describe" disparait avant l'execution
    # des It et leve CommandNotFoundException. Elle capture $script:manifestDirDiscovery
    # au moment de l'appel (defini par le BeforeEach du Describe), pas a la definition.
    function New-DiscoveryManifest {
        param($AppId, $Name, $BuildId, $InstallDir)
        @"
"AppState"
{
	"appid"		"$AppId"
	"name"		"$Name"
	"buildid"		"$BuildId"
	"installdir"		"$InstallDir"
}
"@ | Set-Content -LiteralPath (Join-Path $script:manifestDirDiscovery "appmanifest_$AppId.acf") -Encoding UTF8
    }
}

Describe "Get-KnownGamesRegistry" {
    It "charge le registre reel et expose les 3 jeux connus" {
        $registry = Get-KnownGamesRegistry -RegistryPath $script:realRegistryPath

        $registry.PSObject.Properties.Name | Should -Contain "2394010"
        $registry.PSObject.Properties.Name | Should -Contain "4129620"
        $registry.PSObject.Properties.Name | Should -Contain "896660"
        $registry."2394010".name | Should -Be "palworld"
    }

    It "leve une exception explicite si le fichier de registre est introuvable" {
        { Get-KnownGamesRegistry -RegistryPath (Join-Path $TestDrive "nope.json") } | Should -Throw
    }
}

Describe "Get-AppIdFromManifestFilename" {
    It "extrait l'appid d'un nom de fichier valide" {
        Get-AppIdFromManifestFilename -FileName "appmanifest_2394010.acf" | Should -Be 2394010
    }

    It "retourne `$null pour un nom de fichier qui ne correspond pas au format attendu" {
        Get-AppIdFromManifestFilename -FileName "not-a-manifest.txt" | Should -Be $null
    }
}

Describe "New-RandomPassword" {
    It "genere une chaine de la longueur demandee" {
        (New-RandomPassword -Length 16).Length | Should -Be 16
    }

    It "genere des mots de passe differents a chaque appel (pas une valeur figee)" {
        $p1 = New-RandomPassword -Length 16
        $p2 = New-RandomPassword -Length 16
        $p1 | Should -Not -Be $p2
    }
}

Describe "Resolve-LaunchArgsTemplate" {
    It "remplace tous les placeholders fournis" {
        $result = Resolve-LaunchArgsTemplate -Template "-name {name} -world {world}" -Params @{ name = "Hephaestos-Valheim"; world = "Dedicated" }

        $result | Should -Be "-name Hephaestos-Valheim -world Dedicated"
    }

    It "laisse un placeholder non fourni tel quel (pas de remplacement partiel silencieux dangereux)" {
        $result = Resolve-LaunchArgsTemplate -Template "-name {name} -password {password}" -Params @{ name = "Hephaestos-Valheim" }

        $result | Should -Be "-name Hephaestos-Valheim -password {password}"
    }

    It "ne modifie rien si le template n'a aucun placeholder" {
        $result = Resolve-LaunchArgsTemplate -Template "-useperfthreads" -Params @{}

        $result | Should -Be "-useperfthreads"
    }
}

Describe "New-GameStartTaskIfMissing" {
    BeforeEach {
        Mock Invoke-RegisterScheduledTask {}
    }

    It "cree la tache si elle n'existe pas deja" {
        Mock Get-ScheduledTask { $null }

        New-GameStartTaskIfMissing -TaskName "PalServer" -Execute "C:\steam\PalServer.exe" -Arguments "-useperfthreads"

        Should -Invoke Invoke-RegisterScheduledTask -Times 1 -ParameterFilter {
            $TaskName -eq "PalServer" -and $Execute -eq "C:\steam\PalServer.exe" -and $Arguments -eq "-useperfthreads"
        }
    }

    It "ne fait rien si la tache existe deja (idempotent)" {
        Mock Get-ScheduledTask { [pscustomobject]@{ TaskName = "PalServer" } }

        New-GameStartTaskIfMissing -TaskName "PalServer" -Execute "C:\steam\PalServer.exe" -Arguments "-useperfthreads"

        Should -Invoke Invoke-RegisterScheduledTask -Times 0
    }

    It "gere un jeu sans arguments de lancement (ex. Windrose)" {
        Mock Get-ScheduledTask { $null }

        New-GameStartTaskIfMissing -TaskName "WindroseServer" -Execute "C:\steam\WindroseServer.exe"

        Should -Invoke Invoke-RegisterScheduledTask -Times 1 -ParameterFilter {
            $TaskName -eq "WindroseServer" -and $Arguments -eq ""
        }
    }
}

Describe "Get-DiscoveredGames" {
    <#
    .NOTES
        Remplace les anciens Describe de l'ex-fonction d'auto-decouverte (config + ecriture atomique) --
        Get-DiscoveredGames est un rapport-seul, sans effet de bord disque : plus de tests
        d'ecriture de config/tache planifiee/registre de jeux connus pour cette fonction
        (elle ne consulte meme plus known-games.json, seuls les manifests + $Cfg.servers
        comptent desormais). L'adoption d'un jeu decouvert se fait cote backend/UI (Lot 2).
    #>
    BeforeEach {
        $script:steamRootDiscovery = Join-Path $TestDrive ([guid]::NewGuid().ToString())
        $script:manifestDirDiscovery = Join-Path $script:steamRootDiscovery "steamapps"
        New-Item -ItemType Directory -Path $script:manifestDirDiscovery -Force | Out-Null

        $script:configPathDiscovery = Join-Path $TestDrive "hephaestos-config-discovery-$([guid]::NewGuid()).json"
    }

    It "retourne les jeux installes absents de la config, ignore le depot partage 228980 et les appids deja configures, sans ecrire la config" {
        New-DiscoveryManifest -AppId 896660 -Name "Valheim dedicated" -BuildId "123" -InstallDir "valheim"
        New-DiscoveryManifest -AppId 228980 -Name "Steamworks Common Redistributables" -BuildId "1" -InstallDir "Steamworks Common Redistributables"
        New-DiscoveryManifest -AppId 2394010 -Name "Palworld" -BuildId "999" -InstallDir "PalServer"

        $existing = [pscustomobject]@{ name = "palworld"; appid = 2394010 }
        $cfg = [pscustomobject]@{ steamcmd_root = $script:steamRootDiscovery; servers = @($existing) }
        $cfg | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $script:configPathDiscovery
        $beforeWrite = (Get-Item -LiteralPath $script:configPathDiscovery).LastWriteTimeUtc

        $result = @(Get-DiscoveredGames -Cfg $cfg)

        $result.Count | Should -Be 1
        $result[0].appid | Should -Be 896660
        $result[0].name | Should -Be "Valheim dedicated"
        $result[0].installdir | Should -Be "valheim"
        $result[0].buildid | Should -Be "123"
        (Get-Item -LiteralPath $script:configPathDiscovery).LastWriteTimeUtc | Should -Be $beforeWrite
    }

    It "retourne un tableau vide si le dossier steamapps n'existe pas" {
        $cfg = [pscustomobject]@{ steamcmd_root = (Join-Path $TestDrive ([guid]::NewGuid().ToString())); servers = @() }

        $result = @(Get-DiscoveredGames -Cfg $cfg)

        $result.Count | Should -Be 0
    }
}
