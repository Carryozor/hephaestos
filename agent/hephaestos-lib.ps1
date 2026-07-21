# hephaestos-lib.ps1 -- bibliotheque coeur de l'agent Hephaestos.
# PowerShell 5.1-compatible : pas de ?., pas de -AsHashtable.
# Toute interpolation d'URL suivie de "?" doit s'ecrire "${Var}?..." --
# sinon PowerShell parse "$Var?" comme le nom de variable "Var?" (vide, sans erreur).

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Get-HephConfig {
    <#
    .SYNOPSIS
        Charge et valide la config JSON de l'agent (api_base, agent_token, servers[]).
    #>
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Config introuvable: $Path"
    }

    $raw = Get-Content -LiteralPath $Path -Raw
    $cfg = $raw | ConvertFrom-Json

    if (-not $cfg.api_base) {
        throw "Config invalide ($Path): champ 'api_base' manquant"
    }
    if (-not $cfg.agent_token) {
        throw "Config invalide ($Path): champ 'agent_token' manquant"
    }
    if ($null -eq $cfg.servers) {
        throw "Config invalide ($Path): champ 'servers' manquant"
    }

    return $cfg
}

function Get-LocalBuildId {
    <#
    .SYNOPSIS
        Extrait le buildid d'un appmanifest_<appid>.acf local. Throw si absent/illisible.
    #>
    param(
        [Parameter(Mandatory)]
        [string]$ManifestPath
    )

    if (-not (Test-Path -LiteralPath $ManifestPath)) {
        throw "Manifest introuvable: $ManifestPath"
    }

    $content = Get-Content -LiteralPath $ManifestPath -Raw

    if ($content -match '"buildid"\s+"(\d+)"') {
        return $Matches[1]
    }

    throw "buildid introuvable dans $ManifestPath"
}

function Get-ManifestPath {
    <#
    .SYNOPSIS
        Chemin conventionnel du manifest Steam pour un appid, sous une racine SteamCMD.
    #>
    param(
        [Parameter(Mandatory)]
        [string]$SteamRoot,

        [Parameter(Mandatory)]
        [int]$AppId
    )

    return Join-Path $SteamRoot "steamapps\appmanifest_$AppId.acf"
}

function Get-InstallDirFromManifest {
    <#
    .SYNOPSIS
        Extrait le champ "installdir" d'un appmanifest_<appid>.acf -- source de verite Steam,
        a ne jamais dupliquer/deviner dans la config (incident 2026-07-14 : un install_dir
        code en dur et desynchronise du vrai nom de dossier a fait creer par steamcmd une
        installation isolee dans un sous-dossier, jamais mise a jour).
    #>
    param(
        [Parameter(Mandatory)]
        [string]$ManifestPath
    )

    if (-not (Test-Path -LiteralPath $ManifestPath)) {
        throw "Manifest introuvable: $ManifestPath"
    }

    $content = Get-Content -LiteralPath $ManifestPath -Raw

    if ($content -match '"installdir"\s+"([^"]+)"') {
        return $Matches[1]
    }

    throw "installdir introuvable dans $ManifestPath"
}

function Get-ServerInstallDir {
    <#
    .SYNOPSIS
        Chemin d'installation reel d'un serveur, derive du manifest (jamais code en dur).
    #>
    param(
        [Parameter(Mandatory)]
        [string]$SteamRoot,

        [Parameter(Mandatory)]
        [int]$AppId
    )

    $manifestPath = Get-ManifestPath -SteamRoot $SteamRoot -AppId $AppId
    $installDirName = Get-InstallDirFromManifest -ManifestPath $manifestPath
    return Join-Path $SteamRoot "steamapps\common\$installDirName"
}

function Get-KnownGamesRegistry {
    <#
    .SYNOPSIS
        Charge le registre statique des jeux connus (agent/known-games.json).
    #>
    param(
        [string]$RegistryPath = (Join-Path $PSScriptRoot "known-games.json")
    )

    if (-not (Test-Path -LiteralPath $RegistryPath)) {
        throw "Registre de jeux connus introuvable: $RegistryPath"
    }

    return Get-Content -LiteralPath $RegistryPath -Raw | ConvertFrom-Json
}

function Get-AppIdFromManifestFilename {
    <#
    .SYNOPSIS
        Extrait l'appid d'un nom de fichier appmanifest_<appid>.acf. Retourne $null si le
        nom ne correspond pas au format attendu (jamais d'exception -- appele en boucle sur
        des noms de fichiers arbitraires du disque).
    #>
    param(
        [Parameter(Mandatory)]
        [string]$FileName
    )

    if ($FileName -match "^appmanifest_(\d+)\.acf$") {
        return [int]$Matches[1]
    }

    return $null
}

function New-RandomPassword {
    <#
    .SYNOPSIS
        Genere un mot de passe alphanumerique aleatoire (jamais loggue en clair par les
        appelants).
    #>
    param(
        [int]$Length = 16
    )

    $chars = (48..57) + (65..90) + (97..122)
    return -join (Get-Random -InputObject $chars -Count $Length | ForEach-Object { [char]$_ })
}

function Resolve-LaunchArgsTemplate {
    <#
    .SYNOPSIS
        Remplace les placeholders {cle} d'un template par leur valeur dans Params. Un
        placeholder absent de Params reste tel quel (pas de remplacement partiel silencieux
        qui produirait une ligne de commande invalide sans avertissement).
    #>
    param(
        [Parameter(Mandatory)]
        [string]$Template,

        [Parameter(Mandatory)]
        [hashtable]$Params
    )

    $result = $Template
    foreach ($key in $Params.Keys) {
        $result = $result.Replace("{$key}", "$($Params[$key])")
    }
    return $result
}

function Invoke-RegisterScheduledTask {
    <#
    .SYNOPSIS
        Wrapper mockable autour de Register-ScheduledTask (cree la tache de demarrage d'un
        jeu detecte automatiquement). Meme motif que Invoke-Schtasks/Invoke-Taskkill : isole
        l'appel externe pour rester testable sans Windows reel.
    .NOTES
        Utilise Register-ScheduledTask plutot que schtasks.exe : schtasks echoue sur les
        chemins avec espaces (incident Windrose 2026-07-14, ex. "Windrose Dedicated Server"),
        Register-ScheduledTask les gere correctement.

        Priority 1 (PriorityClass Windows "High") : sans -Settings explicite,
        Register-ScheduledTask applique la priorite PAR DEFAUT 7 (BelowNormal). Un
        serveur de jeu dont le thread principal tourne deja sature a ~100% se fait
        alors preempter par n'importe quel processus concurrent (Defender, autosave,
        l'agent lui-meme) -> lag cote joueurs meme quand le CPU total semble faible
        (incident Palworld 19/07/2026). Realtime (0) est volontairement EVITE : ca
        peut faire perdre la main a la souris/au reseau et degrader l'agent Hephaestos +
        RCON + SSH sur la meme machine -- High est le plafond sain pour un process
        qui doit rester bon citoyen du systeme.

        Mapping Priority(TaskScheduler)->PriorityClass(Windows) NON documente
        fiablement par Microsoft et VERIFIE EMPIRIQUEMENT le 19/07/2026 sur cette
        machine (voir script de test dans l'historique) : 0=Normal, 1=High,
        2-3=AboveNormal, 4-6=Normal, 7-8=BelowNormal. Ne pas supposer une table
        "standard" sans re-tester si Windows Update change ce comportement.
    #>
    param(
        [Parameter(Mandatory)]
        [string]$TaskName,

        [Parameter(Mandatory)]
        [string]$Execute,

        [string]$Arguments = ""
    )

    $action = if ($Arguments) {
        New-ScheduledTaskAction -Execute $Execute -Argument $Arguments
    } else {
        New-ScheduledTaskAction -Execute $Execute
    }
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -Priority 1

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
}

function New-GameStartTaskIfMissing {
    <#
    .SYNOPSIS
        Cree la tache planifiee de demarrage d'un jeu si elle n'existe pas deja (idempotent).
        Ne demarre JAMAIS le jeu -- la creation seule ne declenche rien (declencheur ONSTART).
    #>
    param(
        [Parameter(Mandatory)]
        [string]$TaskName,

        [Parameter(Mandatory)]
        [string]$Execute,

        [string]$Arguments = ""
    )

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        return
    }

    Invoke-RegisterScheduledTask -TaskName $TaskName -Execute $Execute -Arguments $Arguments
}

function Update-HephServersFromBackend {
    <#
    .SYNOPSIS
        Applique la section servers poussee par le backend (bloc config du GET /orders).
        Le backend est la source de verite depuis le Lot 1 v2 : l'agent ne modifie plus
        jamais sa liste de serveurs lui-meme. Comparaison par hash (fourni par le
        backend, memorise dans backend_config_hash) ; ecriture atomique tmp+rename.
    #>
    param(
        [Parameter(Mandatory)] $Cfg,
        [Parameter(Mandatory)] [string]$ConfigPath,
        $BackendConfig,
        [string]$LogPath
    )
    if ($null -eq $BackendConfig -or -not $BackendConfig.hash) { return $Cfg }
    $current = ""
    if ($Cfg.PSObject.Properties.Name -contains "backend_config_hash") {
        $current = $Cfg.backend_config_hash
    }
    if ($current -eq $BackendConfig.hash) { return $Cfg }

    $Cfg.servers = @($BackendConfig.servers)
    if ($Cfg.PSObject.Properties.Name -contains "backend_config_hash") {
        $Cfg.backend_config_hash = $BackendConfig.hash
    } else {
        $Cfg | Add-Member -NotePropertyName backend_config_hash -NotePropertyValue $BackendConfig.hash
    }
    $tmpPath = "${ConfigPath}.tmp"
    $Cfg | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $tmpPath
    Move-Item -LiteralPath $tmpPath -Destination $ConfigPath -Force
    Write-HephLog -LogPath $LogPath -Message "config serveurs appliquee depuis le backend (hash $($BackendConfig.hash), $(@($Cfg.servers).Count) serveur(s))"
    return $Cfg
}

function Get-DiscoveredGames {
    <#
    .SYNOPSIS
        Rapport-seul : liste les jeux installes (manifests steamapps) absents de la
        config. Remplace l'ancienne fonction d'auto-decouverte qui ecrivait la config
        localement -- incompatible avec la config poussee par le backend (deux
        ecrivains). L'adoption se fait desormais cote backend/UI (Lot 2).
    #>
    param([Parameter(Mandatory)] $Cfg)

    $found = @()
    $manifestDir = Join-Path $Cfg.steamcmd_root "steamapps"
    if (-not (Test-Path -LiteralPath $manifestDir)) { return $found }
    $configured = @($Cfg.servers | ForEach-Object { $_.appid })
    $files = @(Get-ChildItem -LiteralPath $manifestDir -Filter "appmanifest_*.acf" -ErrorAction SilentlyContinue)
    foreach ($file in $files) {
        $appid = Get-AppIdFromManifestFilename -FileName $file.Name
        if ($null -eq $appid -or $appid -eq 228980 -or $appid -in $configured) { continue }
        $raw = Get-Content -LiteralPath $file.FullName -Raw
        $name = ""; $buildid = ""; $installdir = ""
        if ($raw -match '"name"\s+"([^"]*)"') { $name = $Matches[1] }
        if ($raw -match '"buildid"\s+"([^"]*)"') { $buildid = $Matches[1] }
        if ($raw -match '"installdir"\s+"([^"]*)"') { $installdir = $Matches[1] }
        $found += @{ appid = $appid; name = $name; installdir = $installdir; buildid = $buildid }
    }
    return $found
}

function Invoke-Steamcmd {
    <#
    .SYNOPSIS
        Wrapper mockable autour du binaire steamcmd (deploiement Lot 2). Capture la
        sortie (piege PS : stdout non capture d'une commande native devient la valeur
        de retour de la fonction englobante).
    #>
    param(
        [Parameter(Mandatory)] $Cfg,
        [Parameter(Mandatory)] [string[]]$Arguments
    )
    $output = & $Cfg.steamcmd @Arguments 2>&1 | Out-String
    return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = $output }
}

function Get-ExeCandidates {
    <#
    .SYNOPSIS
        Scanne les .exe d'un dossier d'install (profondeur <= 4), exclut les
        installeurs/redistribuables, retourne au plus 30 chemins RELATIFS tries par
        taille decroissante (le binaire serveur est presque toujours le plus gros).
    #>
    param(
        [Parameter(Mandatory)] [string]$InstallDir,
        [int]$MaxDepth = 4,
        [int]$MaxCount = 30
    )
    if (-not (Test-Path -LiteralPath $InstallDir)) {
        throw "Get-ExeCandidates: dossier d'install introuvable: ${InstallDir}"
    }
    $exclude = 'redist|vcredist|dotnet|dxsetup|crashreport'
    $files = @(Get-ChildItem -LiteralPath $InstallDir -Filter "*.exe" -File -Recurse -Depth $MaxDepth -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch $exclude } |
        Sort-Object -Property Length -Descending |
        Select-Object -First $MaxCount)
    $sep = [IO.Path]::DirectorySeparatorChar
    $root = (Get-Item -LiteralPath $InstallDir).FullName.TrimEnd($sep) + $sep
    return @($files | ForEach-Object { $_.FullName.Substring($root.Length) })
}

function Invoke-InstallGame {
    <#
    .SYNOPSIS
        Ordre install_game : steamcmd app_update dans la bibliotheque PAR DEFAUT
        (jamais +force_install_dir -- incident 14-15/07, cf. Update-GameServer), puis
        verification manifest + scan des exe candidats.
    .NOTES
        Verifications qui peuvent echouer : exit code steamcmd, presence du manifest
        apres coup, au moins un exe candidat. Un "Success" steamcmd sans manifest est
        un echec.
    #>
    param(
        [Parameter(Mandatory)] $Cfg,
        [Parameter(Mandatory)] [int]$AppId
    )
    $res = Invoke-Steamcmd -Cfg $Cfg -Arguments @("+login", "anonymous", "+app_update", "$AppId", "validate", "+quit")
    if ($res.ExitCode -ne 0) {
        $tail = (($res.Output -split "`r?`n" | Where-Object { $_ -ne "" } | Select-Object -Last 5) -join " / ")
        return [pscustomobject]@{ ok = $false; detail = "steamcmd a echoue (exit code $($res.ExitCode)): ${tail}"; exe_candidates = $null }
    }
    $manifestPath = Get-ManifestPath -SteamRoot $Cfg.steamcmd_root -AppId $AppId
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        return [pscustomobject]@{ ok = $false; detail = "manifest absent apres steamcmd (${manifestPath}) -- installation non confirmee"; exe_candidates = $null }
    }
    return Invoke-ScanExe -Cfg $Cfg -AppId $AppId
}

function Invoke-ScanExe {
    <#
    .SYNOPSIS
        Ordre scan_exe (adoption d'un jeu deja installe) : resout le dossier d'install
        depuis le manifest et rapporte les exe candidats. Aussi la 2e moitie
        d'Invoke-InstallGame.
    #>
    param(
        [Parameter(Mandatory)] $Cfg,
        [Parameter(Mandatory)] [int]$AppId
    )
    try {
        $installDir = Get-ServerInstallDir -SteamRoot $Cfg.steamcmd_root -AppId $AppId
        $candidates = @(Get-ExeCandidates -InstallDir $installDir)
    } catch {
        return [pscustomobject]@{ ok = $false; detail = "scan exe impossible: $($_.Exception.Message)"; exe_candidates = $null }
    }
    if ($candidates.Count -eq 0) {
        return [pscustomobject]@{ ok = $false; detail = "aucun exe candidat trouve dans ${installDir}"; exe_candidates = $null }
    }
    return [pscustomobject]@{ ok = $true; detail = "$($candidates.Count) exe candidat(s) dans ${installDir}"; exe_candidates = $candidates }
}

function Invoke-SetupServer {
    <#
    .SYNOPSIS
        Ordre setup_server : valide l'exe choisi (anti-traversal : resolution complete
        puis prefixe contre le dossier d'install, meme pattern que Restore-GameSave),
        cree la tache planifiee (nom = slug), demarre si demande.
    #>
    param(
        [Parameter(Mandatory)] $Cfg,
        [Parameter(Mandatory)] $Order
    )
    $installDir = Get-ServerInstallDir -SteamRoot $Cfg.steamcmd_root -AppId ([int]$Order.appid)
    $sep = [IO.Path]::DirectorySeparatorChar
    $rootFull = [IO.Path]::GetFullPath($installDir).TrimEnd($sep) + $sep
    $exeFull = [IO.Path]::GetFullPath((Join-Path $installDir ([string]$Order.exe_path)))
    if (-not $exeFull.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
        return [pscustomobject]@{ ok = $false; detail = "exe_path '$($Order.exe_path)' hors du dossier d'install" }
    }
    if (-not (Test-Path -LiteralPath $exeFull)) {
        return [pscustomobject]@{ ok = $false; detail = "exe introuvable: ${exeFull}" }
    }
    $arguments = ""
    if ($Order.PSObject.Properties.Name -contains "launch_args" -and $Order.launch_args) {
        $arguments = [string]$Order.launch_args
    }
    New-GameStartTaskIfMissing -TaskName ([string]$Order.task_name) -Execute $exeFull -Arguments $arguments
    if ($Order.PSObject.Properties.Name -contains "start_now" -and $Order.start_now) {
        Start-GameServer -ServerCfg ([pscustomobject]@{
            name = [string]$Order.server; start_task = [string]$Order.task_name
            process = [string]$Order.process })
    }
    return [pscustomobject]@{ ok = $true; detail = "tache planifiee '$($Order.task_name)' prete (exe ${exeFull})" }
}

$script:HephConfigExtensions = @(".ini", ".json", ".cfg", ".txt", ".yml", ".yaml", ".xml",
                                 ".properties", ".lua", ".toml")

function Get-ConfigRoot {
    <#
    .SYNOPSIS
        Resout la racine autorisee pour l'edition de fichiers (Lot 3) : dossier
        d'install (via manifest, comme Update-GameServer) ou dossier de saves (via
        Get-GameSaveDir -- save_dir est TOUJOURS relatif a l'install, meme convention
        que les backups, jamais tel quel). Throw explicite si save demande sans save_dir.
    #>
    param(
        [Parameter(Mandatory)] $Cfg,
        [Parameter(Mandatory)] $ServerCfg,
        [Parameter(Mandatory)] [string]$Root
    )
    if ($Root -eq "save") {
        $saveDir = Get-GameSaveDir -Cfg $Cfg -ServerCfg $ServerCfg
        if (-not $saveDir) {
            throw "Get-ConfigRoot: save_dir non configure pour '$($ServerCfg.name)'"
        }
        return $saveDir
    }
    return Get-ServerInstallDir -SteamRoot $Cfg.steamcmd_root -AppId ([int]$ServerCfg.appid)
}

function Invoke-ListFiles {
    <#
    .SYNOPSIS
        Ordre list_files : arborescence des fichiers de config (whitelist
        d'extensions), profondeur <= 6, <= 500 entrees, chemins relatifs
        normalises en '/' sur le wire.
    #>
    param(
        [Parameter(Mandatory)] $Cfg,
        [Parameter(Mandatory)] $ServerCfg,
        [Parameter(Mandatory)] [string]$Root
    )
    try {
        $rootDir = Get-ConfigRoot -Cfg $Cfg -ServerCfg $ServerCfg -Root $Root
    } catch {
        return [pscustomobject]@{ ok = $false; detail = $_.Exception.Message; files = @() }
    }
    if (-not (Test-Path -LiteralPath $rootDir)) {
        return [pscustomobject]@{ ok = $false; detail = "dossier introuvable: ${rootDir}"; files = @() }
    }
    $sep = [IO.Path]::DirectorySeparatorChar
    $rootFull = (Get-Item -LiteralPath $rootDir).FullName.TrimEnd($sep) + $sep
    $items = @(Get-ChildItem -LiteralPath $rootDir -File -Recurse -Depth 6 -ErrorAction SilentlyContinue |
        Where-Object { $script:HephConfigExtensions -contains $_.Extension.ToLower() } |
        Select-Object -First 500)
    $files = @($items | ForEach-Object {
        $_.FullName.Substring($rootFull.Length).Replace($sep, "/")
    })
    return [pscustomobject]@{ ok = $true; detail = "$($files.Count) fichier(s)"; files = $files }
}

function Invoke-ReadFile {
    <#
    .SYNOPSIS
        Ordre read_file : anti-traversal (GetFullPath + prefixe, meme pattern que
        Invoke-SetupServer), whitelist d'extensions, borne 512 Ko, contenu en
        base64 + sha256.
    #>
    param(
        [Parameter(Mandatory)] $Cfg,
        [Parameter(Mandatory)] $ServerCfg,
        [Parameter(Mandatory)] [string]$Root,
        [Parameter(Mandatory)] [string]$Path
    )
    try {
        $rootDir = Get-ConfigRoot -Cfg $Cfg -ServerCfg $ServerCfg -Root $Root
    } catch {
        return [pscustomobject]@{ ok = $false; detail = $_.Exception.Message }
    }
    $relative = ($Path -replace "/", [IO.Path]::DirectorySeparatorChar)
    $sep = [IO.Path]::DirectorySeparatorChar
    $rootFull = [IO.Path]::GetFullPath($rootDir).TrimEnd($sep) + $sep
    $fileFull = [IO.Path]::GetFullPath((Join-Path $rootDir $relative))
    if (-not $fileFull.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
        return [pscustomobject]@{ ok = $false; detail = "chemin '${Path}' hors du dossier autorise" }
    }
    $ext = [IO.Path]::GetExtension($fileFull).ToLower()
    if ($script:HephConfigExtensions -notcontains $ext) {
        return [pscustomobject]@{ ok = $false; detail = "extension '${ext}' non autorisee" }
    }
    if (-not (Test-Path -LiteralPath $fileFull)) {
        return [pscustomobject]@{ ok = $false; detail = "fichier introuvable: ${fileFull}" }
    }
    $size = (Get-Item -LiteralPath $fileFull).Length
    if ($size -gt 524288) {
        return [pscustomobject]@{ ok = $false; detail = "fichier trop volumineux (${size} octets, max 512 Ko)" }
    }
    $bytes = [IO.File]::ReadAllBytes($fileFull)
    $sha256 = (Get-FileHash -LiteralPath $fileFull -Algorithm SHA256).Hash.ToLower()
    return [pscustomobject]@{ ok = $true; detail = "lu (${size} octets)"
        content_b64 = [Convert]::ToBase64String($bytes); sha256 = $sha256 }
}

function Invoke-WriteFile {
    <#
    .SYNOPSIS
        Ordre write_file : anti-traversal (meme pattern que Invoke-ReadFile),
        refus sur conflit sha256 (aucune ecriture, aucun .hephaestos-bak), sinon backup
        .hephaestos-bak (ecrase a chaque fois) puis ecriture atomique tmp+rename (meme
        idiome que Update-HephServersFromBackend).
    #>
    param(
        [Parameter(Mandatory)] $Cfg,
        [Parameter(Mandatory)] $ServerCfg,
        [Parameter(Mandatory)] [string]$Root,
        [Parameter(Mandatory)] [string]$Path,
        [Parameter(Mandatory)] [string]$ContentB64,
        [Parameter(Mandatory)] [string]$ExpectedSha256
    )
    try {
        $rootDir = Get-ConfigRoot -Cfg $Cfg -ServerCfg $ServerCfg -Root $Root
    } catch {
        return [pscustomobject]@{ ok = $false; detail = $_.Exception.Message }
    }
    $relative = ($Path -replace "/", [IO.Path]::DirectorySeparatorChar)
    $sep = [IO.Path]::DirectorySeparatorChar
    $rootFull = [IO.Path]::GetFullPath($rootDir).TrimEnd($sep) + $sep
    $fileFull = [IO.Path]::GetFullPath((Join-Path $rootDir $relative))
    if (-not $fileFull.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
        return [pscustomobject]@{ ok = $false; detail = "chemin '${Path}' hors du dossier autorise" }
    }
    $ext = [IO.Path]::GetExtension($fileFull).ToLower()
    if ($script:HephConfigExtensions -notcontains $ext) {
        return [pscustomobject]@{ ok = $false; detail = "extension '${ext}' non autorisee" }
    }
    if (-not (Test-Path -LiteralPath $fileFull)) {
        return [pscustomobject]@{ ok = $false; detail = "fichier introuvable: ${fileFull}" }
    }
    $currentSha = (Get-FileHash -LiteralPath $fileFull -Algorithm SHA256).Hash.ToLower()
    if ($currentSha -ne $ExpectedSha256.ToLower()) {
        return [pscustomobject]@{ ok = $false
            detail = "le fichier a change depuis sa lecture (conflit) -- ecriture refusee" }
    }
    Copy-Item -LiteralPath $fileFull -Destination "${fileFull}.hephaestos-bak" -Force
    $bytes = [Convert]::FromBase64String($ContentB64)
    $tmpPath = "${fileFull}.tmp"
    [IO.File]::WriteAllBytes($tmpPath, $bytes)
    Move-Item -LiteralPath $tmpPath -Destination $fileFull -Force
    return [pscustomobject]@{ ok = $true; detail = "ecrit ($($bytes.Length) octets), backup .hephaestos-bak cree" }
}

function Get-PublicBuildId {
    <#
    .SYNOPSIS
        Interroge api.steamcmd.net pour le buildid public courant d'un appid.
    #>
    param(
        [Parameter(Mandatory)]
        [int]$AppId
    )

    $url = "https://api.steamcmd.net/v1/info/${AppId}"

    try {
        $resp = Invoke-RestMethod -Uri $url -Method Get
    } catch {
        throw "Echec appel api.steamcmd.net pour appid ${AppId}: $($_.Exception.Message)"
    }

    if ($null -eq $resp -or $null -eq $resp.data) {
        throw "Reponse steamcmd.net invalide pour appid ${AppId}"
    }

    $appData = $resp.data.$AppId
    if ($null -eq $appData) {
        throw "Reponse steamcmd.net sans donnees pour appid ${AppId}"
    }

    $buildid = $appData.depots.branches.public.buildid
    if (-not $buildid) {
        throw "buildid public introuvable pour appid ${AppId}"
    }

    return [string]$buildid
}

function Send-KumaPush {
    <#
    .SYNOPSIS
        Envoie un heartbeat push vers Uptime Kuma (status up/down, message).
    .NOTES
        Piege PS 13/07 #1 : "?" apres une variable interpolee doit s'ecrire ${Var}?...
        Piege PS 13/07 #2 : le message doit etre URL-encode (espaces -> %20, etc).
    #>
    param(
        [Parameter(Mandatory)]
        [string]$PushUrl,

        [Parameter(Mandatory)]
        [string]$Status,

        [string]$Msg = ""
    )

    $encodedMsg = [Uri]::EscapeDataString($Msg)
    $uri = "${PushUrl}?status=${Status}&msg=${encodedMsg}"

    try {
        return Invoke-RestMethod -Uri $uri -Method Get
    } catch {
        throw "Echec push Kuma vers ${PushUrl}: $($_.Exception.Message)"
    }
}

function Invoke-HephApi {
    <#
    .SYNOPSIS
        Appelle l'API backend Hephaestos en Bearer agent_token, corps JSON optionnel.
    #>
    param(
        [Parameter(Mandatory)]
        [pscustomobject]$Cfg,

        [Parameter(Mandatory)]
        [string]$Method,

        [Parameter(Mandatory)]
        [string]$Path,

        $Body = $null
    )

    if (-not $Cfg.api_base) {
        throw "Invoke-HephApi: Cfg.api_base manquant"
    }
    if (-not $Cfg.agent_token) {
        throw "Invoke-HephApi: Cfg.agent_token manquant"
    }

    $uri = "$($Cfg.api_base)${Path}"
    $headers = @{ Authorization = "Bearer $($Cfg.agent_token)" }

    $params = @{
        Uri     = $uri
        Method  = $Method
        Headers = $headers
    }

    if ($null -ne $Body) {
        $params.Body = ($Body | ConvertTo-Json -Depth 10)
        $params.ContentType = "application/json"
    }

    try {
        return Invoke-RestMethod @params
    } catch {
        throw "Echec appel API Hephaestos ${Method} ${Path}: $($_.Exception.Message)"
    }
}

function New-RconPacket {
    <#
    .SYNOPSIS
        Construit un paquet Source RCON (longueur int32 LE, id, type, body, 2 null bytes).
    .NOTES
        Format verifie le 13/07 contre le vrai serveur Palworld 203.0.113.10:25575
        (probe Python) : longueur = id(4) + type(4) + body(N) + 2 null terminators,
        n'inclut PAS le champ longueur lui-meme.
    #>
    param(
        [Parameter(Mandatory)]
        [int]$Id,

        [Parameter(Mandatory)]
        [int]$Type,

        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$Body
    )

    $bodyBytes = [Text.Encoding]::ASCII.GetBytes($Body)
    $length = 4 + 4 + $bodyBytes.Length + 1 + 1

    $ms = New-Object System.IO.MemoryStream
    $bw = New-Object System.IO.BinaryWriter($ms)
    try {
        $bw.Write([int32]$length)
        $bw.Write([int32]$Id)
        $bw.Write([int32]$Type)
        if ($bodyBytes.Length -gt 0) {
            $bw.Write($bodyBytes)
        }
        $bw.Write([byte]0)
        $bw.Write([byte]0)
        $bw.Flush()
        return $ms.ToArray()
    } finally {
        $bw.Dispose()
    }
}

function Read-RconPacket {
    <#
    .SYNOPSIS
        Lit un paquet Source RCON complet depuis un Stream et retourne {Id, Type, Body}.
    #>
    param(
        [Parameter(Mandatory)]
        $Stream
    )

    $lengthBytes = New-Object byte[] 4
    $offset = 0
    while ($offset -lt 4) {
        $n = $Stream.Read($lengthBytes, $offset, 4 - $offset)
        if ($n -le 0) {
            throw "RCON: reponse incomplete (champ longueur)"
        }
        $offset += $n
    }
    $length = [BitConverter]::ToInt32($lengthBytes, 0)

    $payload = New-Object byte[] $length
    $offset = 0
    while ($offset -lt $length) {
        $n = $Stream.Read($payload, $offset, $length - $offset)
        if ($n -le 0) {
            throw "RCON: reponse tronquee"
        }
        $offset += $n
    }

    $id = [BitConverter]::ToInt32($payload, 0)
    $type = [BitConverter]::ToInt32($payload, 4)
    $bodyLength = $length - 4 - 4 - 2
    $body = ""
    if ($bodyLength -gt 0) {
        $body = [Text.Encoding]::ASCII.GetString($payload, 8, $bodyLength)
    }

    return [pscustomobject]@{
        Id   = $id
        Type = $type
        Body = $body
    }
}

function New-RconClient {
    <#
    .SYNOPSIS
        Cree un TcpClient configure avec des timeouts Send/Receive, sans se connecter.
    .NOTES
        Fix 2026-07-13 (2e passe) : sans timeout explicite, un serveur qui accepte la socket
        sans jamais repondre (ou un hote injoignable au niveau applicatif) bloque l'agent
        indefiniment. Separe de Invoke-Rcon pour rester testable sans reseau reel : la
        configuration des timeouts ne necessite pas de Connect().
    #>
    param(
        [int]$TimeoutMs = 5000
    )

    $client = New-Object System.Net.Sockets.TcpClient
    $client.SendTimeout = $TimeoutMs
    $client.ReceiveTimeout = $TimeoutMs
    return $client
}

function Invoke-Rcon {
    <#
    .SYNOPSIS
        Authentifie puis execute une commande via Source RCON, retourne le corps de reponse.
    .NOTES
        $Stream est un point d'injection pour les tests (paquet AUTH/EXEC verifie sans reseau
        reel) ; en usage normal, laisser $null pour ouvrir une vraie connexion TCP via
        New-RconClient (timeouts Send/Receive/Read/Write appliques, $TimeoutMs configurable).

        Fix 2026-07-13 (2e passe) : certains serveurs RCON Source envoient un paquet
        SERVERDATA_RESPONSE_VALUE (type=0) vide juste apres l'auth, AVANT le vrai
        SERVERDATA_AUTH_RESPONSE (type=2) qui porte le id reel (-1 si refuse). On ignore
        tout paquet qui n'est pas de type 2, avec une limite de lecture pour ne pas boucler
        indefiniment sur un flux corrompu. Le cas deja valide en live (type=2 immediat) reste
        compatible : premiere iteration de la boucle.
    #>
    param(
        [Parameter(Mandatory)]
        [string]$RconHost,

        [Parameter(Mandatory)]
        [int]$Port,

        [Parameter(Mandatory)]
        [string]$Password,

        [Parameter(Mandatory)]
        [string]$Command,

        [int]$TimeoutMs = 5000,

        $Stream = $null
    )

    $client = $null
    $ownsStream = $false

    if ($null -eq $Stream) {
        $client = New-RconClient -TimeoutMs $TimeoutMs
        $client.Connect($RconHost, $Port)
        $Stream = $client.GetStream()
        if ($Stream.CanTimeout) {
            $Stream.ReadTimeout = $TimeoutMs
            $Stream.WriteTimeout = $TimeoutMs
        }
        $ownsStream = $true
    }

    try {
        $authPacket = New-RconPacket -Id 1 -Type 3 -Body $Password
        $Stream.Write($authPacket, 0, $authPacket.Length)

        $maxAuthPackets = 3
        $authResp = $null
        for ($i = 0; $i -lt $maxAuthPackets; $i++) {
            $pkt = Read-RconPacket -Stream $Stream
            if ($pkt.Type -eq 2) {
                $authResp = $pkt
                break
            }
        }

        if ($null -eq $authResp) {
            throw "RCON: pas de SERVERDATA_AUTH_RESPONSE (type=2) recu apres $maxAuthPackets paquets"
        }

        if ($authResp.Id -eq -1) {
            throw "RCON auth refused"
        }

        $execPacket = New-RconPacket -Id 2 -Type 2 -Body $Command
        $Stream.Write($execPacket, 0, $execPacket.Length)
        $execResp = Read-RconPacket -Stream $Stream

        return $execResp.Body
    } finally {
        if ($ownsStream -and $client) {
            $client.Close()
        }
    }
}

function Get-PalworldAdminPassword {
    <#
    .SYNOPSIS
        Extrait AdminPassword depuis un fichier PalWorldSettings.ini.
    #>
    param(
        [Parameter(Mandatory)]
        [string]$SettingsIni
    )

    if (-not (Test-Path -LiteralPath $SettingsIni)) {
        throw "Fichier de settings introuvable: $SettingsIni"
    }

    $content = Get-Content -LiteralPath $SettingsIni -Raw

    if ($content -match 'AdminPassword="([^"]*)"') {
        return $Matches[1]
    }

    throw "AdminPassword introuvable dans $SettingsIni"
}

function Get-PalworldPlayers {
    <#
    .SYNOPSIS
        Liste et compte les joueurs connectes via RCON ShowPlayers.
    .NOTES
        Retourne TOUJOURS un objet {Count, Players} (jamais $null lui-meme) : Count est un
        entier (0 si aucun joueur), Players un tableau d'objets {name, playeruid, steamid}
        extraits des colonnes CSV (deja presentes dans ShowPlayers, aucun appel RCON
        supplementaire necessaire pour le SteamID).
    #>
    param(
        [Parameter(Mandatory)]
        $Cfg,

        [Parameter(Mandatory)]
        $ServerCfg
    )

    $installDir = Get-ServerInstallDir -SteamRoot $Cfg.steamcmd_root -AppId $ServerCfg.appid
    $settingsIni = Join-Path $installDir "Pal\Saved\Config\WindowsServer\PalWorldSettings.ini"
    $password = Get-PalworldAdminPassword -SettingsIni $settingsIni

    $csv = Invoke-Rcon -RconHost $ServerCfg.rcon.host -Port $ServerCfg.rcon.port -Password $password -Command "ShowPlayers"

    $lines = @($csv -split "`n" | Where-Object { $_.Trim() -ne "" })

    if ($lines.Count -le 1) {
        return [pscustomobject]@{ Count = 0; Players = @() }
    }

    $players = @()
    foreach ($line in $lines[1..($lines.Count - 1)]) {
        $fields = $line -split ","
        if ($fields.Count -ge 3) {
            $players += [pscustomobject]@{
                name      = $fields[0]
                playeruid = $fields[1]
                steamid   = $fields[2]
            }
        }
    }

    return [pscustomobject]@{ Count = $players.Count; Players = $players }
}

function Get-ServerRconInfo {
    <#
    .SYNOPSIS
        Recupere les infos serveur brutes via la commande RCON "Info" (Palworld
        uniquement -- retourne $null sans tenter de connexion pour tout serveur
        sans configuration rcon, ex. Windrose/Valheim).
    #>
    param(
        [Parameter(Mandatory)]
        $Cfg,

        [Parameter(Mandatory)]
        $ServerCfg
    )

    if (-not ($ServerCfg.PSObject.Properties.Name -contains "rcon" -and $ServerCfg.rcon)) {
        return $null
    }

    $installDir = Get-ServerInstallDir -SteamRoot $Cfg.steamcmd_root -AppId $ServerCfg.appid
    $settingsIni = Join-Path $installDir "Pal\Saved\Config\WindowsServer\PalWorldSettings.ini"
    $password = Get-PalworldAdminPassword -SettingsIni $settingsIni

    return Invoke-Rcon -RconHost $ServerCfg.rcon.host -Port $ServerCfg.rcon.port -Password $password -Command "Info"
}

function Get-ProcessMetrics {
    <#
    .SYNOPSIS
        CPU (%, ramene sur l'echelle 0-100 par le nombre de coeurs logiques) et
        RAM (Mo) du process serveur. $null pour les deux champs si le process
        est introuvable ou si la requete CIM echoue -- jamais d'exception non
        capturee.
    .NOTES
        Win32_PerfFormattedData_PerfProc_Process expose un pourcentage deja
        calcule par Windows (pas besoin de deux echantillons espaces comme
        avec Get-Counter brut) MAIS ne le normalise PAS par le nombre de
        coeurs -- un process multithread peut y depasser 100% sur une machine
        multicoeur (convention Perfmon/`top` classique, differente de celle du
        Gestionnaire des taches moderne). Sans cette division, un serveur
        utilisant 1,5 coeur sur une machine a 6 coeurs remonte "156%", lu a
        tort comme une anomalie -- corrige le 2026-07-14 apres un signalement
        en prod (Palworld affichait 156% sur une machine a 6 coeurs).
    #>
    param(
        [Parameter(Mandatory)]
        [string]$ProcessName
    )

    try {
        $proc = Get-Process -Name $ProcessName -ErrorAction Stop
    } catch {
        return [pscustomobject]@{ CpuPercent = $null; MemMb = $null }
    }

    $memMb = [math]::Round($proc.WorkingSet64 / 1MB, 1)

    $cpuPercent = $null
    try {
        $perf = Get-CimInstance -ClassName Win32_PerfFormattedData_PerfProc_Process -Filter "Name = '$ProcessName'" -ErrorAction Stop
        $cores = (Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop).NumberOfLogicalProcessors
        if ($perf -and $cores) {
            $rawPercent = [double]((@($perf) | Select-Object -First 1).PercentProcessorTime)
            $cpuPercent = [math]::Round($rawPercent / $cores, 1)
        }
    } catch {
        $cpuPercent = $null
    }

    return [pscustomobject]@{ CpuPercent = $cpuPercent; MemMb = $memMb }
}

function Get-WindrosePlayers {
    <#
    .SYNOPSIS
        Liste et compte les joueurs connectes via le fichier de statut ecrit par le mod
        WindrosePlus (pas de RCON necessaire -- simple lecture fichier, deja tenu a jour
        par le mod).
    .NOTES
        Retourne TOUJOURS un objet {Count, Players}. Count est $null (pas 0) si l'info est
        indisponible (fichier absent, JSON invalide, format inattendu) -- distinct de 0 qui
        signifie "aucun joueur connecte". Players est toujours un tableau (jamais $null
        lui-meme, meme quand Count est $null) pour eviter aux appelants un double test null.
        steamid est toujours $null ici : WindrosePlus n'expose pas de SteamID verifie a ce
        jour (voir docs/specs/2026-07-14-player-details-design.md, hors scope de cette phase).
    #>
    param(
        [Parameter(Mandatory)]
        $Cfg,

        [Parameter(Mandatory)]
        $ServerCfg
    )

    $installDir = Get-ServerInstallDir -SteamRoot $Cfg.steamcmd_root -AppId $ServerCfg.appid
    $statusPath = Join-Path $installDir "windrose_plus_data\server_status.json"

    if (-not (Test-Path -LiteralPath $statusPath)) {
        return [pscustomobject]@{ Count = $null; Players = @() }
    }

    try {
        $raw = Get-Content -LiteralPath $statusPath -Raw -ErrorAction Stop
        $data = $raw | ConvertFrom-Json -ErrorAction Stop
    } catch {
        return [pscustomobject]@{ Count = $null; Players = @() }
    }

    if (-not $data.server -or $null -eq $data.server.player_count) {
        return [pscustomobject]@{ Count = $null; Players = @() }
    }

    $players = @()
    if ($data.players) {
        foreach ($p in @($data.players)) {
            $players += [pscustomobject]@{
                name       = $p.name
                session_id = $p.session_id
                steamid    = $null
            }
        }
    }

    return [pscustomobject]@{ Count = [int]$data.server.player_count; Players = $players }
}

function ConvertFrom-A2sInfoPlayers {
    <#
    .SYNOPSIS
        Extrait le nombre de joueurs d'une reponse A2S_INFO (0x49). Retourne $null si la
        reponse n'est pas exploitable (jamais d'exception : parseur pur, testable).
    .NOTES
        Format A2S_INFO : FF FF FF FF 49 <protocol:1> <name\0><map\0><folder\0><game\0>
        <appid:2> <players:1> ... -- on saute les 4 chaines null-terminees puis le short
        appid, l'octet suivant est le compte de joueurs.
    #>
    param(
        [Parameter(Mandatory)]
        [byte[]]$Response
    )

    if ($Response.Length -lt 10 -or $Response[4] -ne 0x49) {
        return $null
    }

    $offset = 6  # apres header(4) + type(1) + protocol(1)
    for ($s = 0; $s -lt 4; $s++) {
        while ($offset -lt $Response.Length -and $Response[$offset] -ne 0) { $offset++ }
        $offset++
        if ($offset -ge $Response.Length) { return $null }
    }
    $offset += 2  # short appid
    if ($offset -ge $Response.Length) { return $null }
    return [int]$Response[$offset]
}

function Get-A2sPlayerCount {
    <#
    .SYNOPSIS
        Nombre de joueurs via une requete A2S_INFO UDP (jeux Source query, ex. Valheim :
        query_port = port de jeu + 1). Retourne $null en cas d'echec (timeout, reponse
        invalide) -- $null = "inconnu", jamais 0 par defaut.
    .NOTES
        Gere le handshake anti-spoof moderne : si le serveur repond S2C_CHALLENGE (0x41),
        la requete est re-emise avec les 4 octets de challenge en suffixe.
    #>
    param(
        [Parameter(Mandatory)]
        [string]$HostName,

        [Parameter(Mandatory)]
        [int]$Port,

        [int]$TimeoutMs = 2000
    )

    $baseQuery = [byte[]](0xFF, 0xFF, 0xFF, 0xFF, 0x54) +
        [System.Text.Encoding]::ASCII.GetBytes("Source Engine Query") + [byte[]](0x00)

    $udp = New-Object System.Net.Sockets.UdpClient
    try {
        $udp.Client.ReceiveTimeout = $TimeoutMs
        $udp.Connect($HostName, $Port)
        $udp.Send($baseQuery, $baseQuery.Length) | Out-Null
        $remoteEp = New-Object System.Net.IPEndPoint ([System.Net.IPAddress]::Any, 0)
        $resp = $udp.Receive([ref]$remoteEp)

        if ($resp.Length -ge 9 -and $resp[4] -eq 0x41) {
            $challenged = $baseQuery + $resp[5..8]
            $udp.Send($challenged, $challenged.Length) | Out-Null
            $resp = $udp.Receive([ref]$remoteEp)
        }

        return ConvertFrom-A2sInfoPlayers -Response $resp
    } catch {
        return $null
    } finally {
        $udp.Close()
    }
}

function Get-GameSaveDir {
    <#
    .SYNOPSIS
        Chemin absolu du dossier de saves du serveur (installDir + save_dir relatif de la
        config), ou $null si save_dir n'est pas configure (backups desactives).
    #>
    param(
        [Parameter(Mandatory)]
        $Cfg,

        [Parameter(Mandatory)]
        $ServerCfg
    )

    if (-not ($ServerCfg.PSObject.Properties.Name -contains "save_dir") -or -not $ServerCfg.save_dir) {
        return $null
    }
    $installDir = Get-ServerInstallDir -SteamRoot $Cfg.steamcmd_root -AppId $ServerCfg.appid
    return Join-Path $installDir $ServerCfg.save_dir
}

function Get-SaveBackupDir {
    <#
    .SYNOPSIS
        Dossier des backups zip de CE serveur (backup_root de la config, defaut
        <steamcmd_root>\hephaestos-backups, + sous-dossier au nom du serveur).
    #>
    param(
        [Parameter(Mandatory)]
        $Cfg,

        [Parameter(Mandatory)]
        $ServerCfg
    )

    $root = if ($Cfg.PSObject.Properties.Name -contains "backup_root" -and $Cfg.backup_root) {
        $Cfg.backup_root
    } else {
        Join-Path $Cfg.steamcmd_root "hephaestos-backups"
    }
    return Join-Path $root $ServerCfg.name
}

function Backup-GameSave {
    <#
    .SYNOPSIS
        Zippe le dossier de saves vers <backup_dir>\<UTC yyyyMMdd-HHmmss>-<Kind>.zip,
        purge au-dela de backup_keep (defaut 10, les plus recents gardes), retourne le
        nom du fichier cree.
    .NOTES
        Verifications qui peuvent echouer : dossier de saves existant ET zip reellement
        produit sur disque (jamais confiance au seul retour de Compress-Archive).
        Le tri de purge s'appuie sur le NOM (timestamp UTC prefixe, lexicographiquement
        chronologique), pas sur les dates filesystem.
    #>
    param(
        [Parameter(Mandatory)]
        $Cfg,

        [Parameter(Mandatory)]
        $ServerCfg,

        [string]$Kind = "manual"
    )

    $saveDir = Get-GameSaveDir -Cfg $Cfg -ServerCfg $ServerCfg
    if (-not $saveDir) {
        throw "Backup-GameSave: save_dir non configure pour '$($ServerCfg.name)'"
    }
    if (-not (Test-Path -LiteralPath $saveDir)) {
        throw "Backup-GameSave: dossier de saves introuvable: ${saveDir}"
    }

    $backupDir = Get-SaveBackupDir -Cfg $Cfg -ServerCfg $ServerCfg
    if (-not (Test-Path -LiteralPath $backupDir)) {
        New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    }

    $file = "$((Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss'))-${Kind}.zip"
    $dest = Join-Path $backupDir $file
    Compress-Archive -Path (Join-Path $saveDir "*") -DestinationPath $dest -Force

    if (-not (Test-Path -LiteralPath $dest)) {
        throw "Backup-GameSave: le zip n'a pas ete produit: ${dest}"
    }

    $keep = 10
    if ($Cfg.PSObject.Properties.Name -contains "backup_keep" -and $Cfg.backup_keep) {
        $keep = [int]$Cfg.backup_keep
    }
    Get-ChildItem -LiteralPath $backupDir -Filter "*.zip" |
        Sort-Object -Property Name -Descending |
        Select-Object -Skip $keep |
        Remove-Item -Force

    return $file
}

function Get-GameSaveBackups {
    <#
    .SYNOPSIS
        Liste les backups zip du serveur, plus recent d'abord : tableau de
        @{file; size_mb; created}. Vide (jamais $null, jamais d'exception) si aucun.
    #>
    param(
        [Parameter(Mandatory)]
        $Cfg,

        [Parameter(Mandatory)]
        $ServerCfg
    )

    $backupDir = Get-SaveBackupDir -Cfg $Cfg -ServerCfg $ServerCfg
    if (-not (Test-Path -LiteralPath $backupDir)) {
        return @()
    }

    return @(Get-ChildItem -LiteralPath $backupDir -Filter "*.zip" |
        Sort-Object -Property Name -Descending |
        ForEach-Object {
            @{
                file    = $_.Name
                size_mb = [math]::Round($_.Length / 1MB, 1)
                created = $_.LastWriteTimeUtc.ToString("o")
            }
        })
}

function Restore-GameSave {
    <#
    .SYNOPSIS
        Restaure un backup zip : stop serveur -> copie de surete de la save actuelle
        (pre-restore) -> remplacement du dossier de saves -> start. Retourne un detail
        lisible pour l'ordre.
    .NOTES
        Le nom du backup vient d'un ordre reseau : valide STRICTEMENT (pas de separateur
        de chemin -- anti-traversal) et doit exister dans le dossier de backups du
        serveur. La copie de surete est best-effort : son echec n'annule pas la
        restauration (la save courante est precisement celle qu'on veut remplacer),
        mais il est signale dans le detail.
    #>
    param(
        [Parameter(Mandatory)]
        $Cfg,

        [Parameter(Mandatory)]
        $ServerCfg,

        [Parameter(Mandatory)]
        [string]$BackupFile
    )

    if ($BackupFile -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]*\.zip$') {
        throw "Restore-GameSave: nom de backup invalide: ${BackupFile}"
    }

    $backupPath = Join-Path (Get-SaveBackupDir -Cfg $Cfg -ServerCfg $ServerCfg) $BackupFile
    if (-not (Test-Path -LiteralPath $backupPath)) {
        throw "Restore-GameSave: backup introuvable: ${backupPath}"
    }

    $saveDir = Get-GameSaveDir -Cfg $Cfg -ServerCfg $ServerCfg
    if (-not $saveDir) {
        throw "Restore-GameSave: save_dir non configure pour '$($ServerCfg.name)'"
    }

    Stop-GameServer -Cfg $Cfg -ServerCfg $ServerCfg -Reason "Restauration"

    $safetyNote = ""
    if (Test-Path -LiteralPath $saveDir) {
        try {
            $safety = Backup-GameSave -Cfg $Cfg -ServerCfg $ServerCfg -Kind "pre-restore"
            $safetyNote = ", save precedente conservee dans ${safety}"
        } catch {
            $safetyNote = ", ATTENTION copie de surete impossible: $($_.Exception.Message)"
        }
        Remove-Item -Path (Join-Path $saveDir "*") -Recurse -Force
    } else {
        New-Item -ItemType Directory -Path $saveDir -Force | Out-Null
    }

    Expand-Archive -LiteralPath $backupPath -DestinationPath $saveDir -Force

    if (-not (Get-ChildItem -LiteralPath $saveDir | Select-Object -First 1)) {
        throw "Restore-GameSave: dossier de saves vide apres extraction de ${BackupFile}"
    }

    Start-GameServer -ServerCfg $ServerCfg

    return "restauration de ${BackupFile} effectuee${safetyNote}"
}

function Get-InstalledWorkshopMods {
    <#
    .SYNOPSIS
        Liste les IDs Workshop actuellement installes (sous-dossiers de Mods\Workshop\).
    .NOTES
        Retourne un tableau vide (jamais $null, jamais d'exception) si le dossier
        Mods\Workshop n'existe pas encore -- cas normal avant le premier mod installe.
    #>
    param(
        [Parameter(Mandatory)]
        $Cfg,

        [Parameter(Mandatory)]
        $ServerCfg
    )

    $installDir = Get-ServerInstallDir -SteamRoot $Cfg.steamcmd_root -AppId $ServerCfg.appid
    $workshopDir = Join-Path $installDir "Mods\Workshop"

    if (-not (Test-Path -LiteralPath $workshopDir)) {
        return @()
    }

    return @(Get-ChildItem -LiteralPath $workshopDir -Directory | Select-Object -ExpandProperty Name)
}

function Install-WorkshopMod {
    <#
    .SYNOPSIS
        Telecharge un mod Steam Workshop via steamcmd et le copie dans Mods\Workshop\<id>.
    .NOTES
        Meme convention d'appel que Update-GameServer : invocation directe de $Cfg.steamcmd
        via l'operateur &, sortie capturee explicitement (2>&1 | Out-String) pour eviter
        qu'une sortie stdout non capturee ne devienne la valeur de retour de la fonction
        (meme piege documente sur Update-GameServer, fix F1 du 13/07).
        Utilise Cfg.steamcmd_login s'il est defini (compte Steam reel), sinon "anonymous"
        par defaut. Incident 2026-07-14 : le login anonyme echoue systematiquement pour
        workshop_download_item sur Palworld (steamcmd repond exit 0 mais "ERROR! Download
        item X failed (Failure)." et ne produit aucun dossier) -- l'editeur n'a pas active
        "anonymous game servers can download workshop items" dans Steamworks pour ce jeu.
        Le compte reel (deja authentifie une fois via Steam Guard, credentials mis en cache
        par steamcmd lui-meme) contourne cette limitation.
        Verifie la presence reelle du dossier de destination apres la copie -- ne fait
        jamais confiance au seul code de sortie de steamcmd/Copy-Item (verification qui
        peut echouer).
    #>
    param(
        [Parameter(Mandatory)]
        $Cfg,

        [Parameter(Mandatory)]
        $ServerCfg,

        [Parameter(Mandatory)]
        [string]$WorkshopId
    )

    $login = if ($Cfg.PSObject.Properties.Name -contains "steamcmd_login" -and $Cfg.steamcmd_login) {
        $Cfg.steamcmd_login
    } else {
        "anonymous"
    }

    $steamArgs = @(
        "+login", $login,
        "+workshop_download_item", "$($ServerCfg.workshop_appid)", $WorkshopId,
        "+quit"
    )
    $steamOutput = & $Cfg.steamcmd @steamArgs 2>&1 | Out-String
    $steamExitCode = $LASTEXITCODE

    # Le cache de credentials steamcmd (compte reel + Steam Guard) peut expirer : le
    # symptome brut ("FAILED (Invalid Password)" etc.) est cryptique dans le detail
    # d'ordre remonte au dashboard. Message actionnable plutot que le tail generique.
    # Motifs volontairement restreints aux messages d'ECHEC explicites : "Steam Guard"
    # ou "Two-factor" seuls apparaissent aussi dans des prompts/bannieres de sorties
    # par ailleurs reussies (revue 15/07) et donneraient de faux rejets.
    if ($steamOutput -match "Invalid Password|Login Failure|Invalid Login Auth Code|Account Logon Denied|FAILED \(Auth") {
        throw "echec d'authentification steamcmd pour le login '${login}' -- le cache de credentials a probablement expire : se reconnecter une fois interactivement sur la machine (steamcmd +login ${login}, code Steam Guard), puis relancer l'installation du mod ${WorkshopId}"
    }

    if ($steamExitCode -ne 0) {
        $tail = (($steamOutput -split "`r?`n" | Where-Object { $_ -ne "" } | Select-Object -Last 5) -join " / ")
        throw "steamcmd a echoue pour le mod ${WorkshopId} (exit code ${steamExitCode}): ${tail}"
    }

    $downloadedDir = Join-Path $Cfg.steamcmd_root "steamapps\workshop\content\$($ServerCfg.workshop_appid)\$WorkshopId"
    if (-not (Test-Path -LiteralPath $downloadedDir)) {
        throw "steamcmd n'a pas produit le dossier attendu pour le mod ${WorkshopId} (workshop_download_item a peut-etre echoue)"
    }

    $installDir = Get-ServerInstallDir -SteamRoot $Cfg.steamcmd_root -AppId $ServerCfg.appid
    $destinationDir = Join-Path $installDir "Mods\Workshop\$WorkshopId"

    # Si le mod est deja installe (reinstall/rafraichissement), Copy-Item vers un dossier
    # destination existant copierait la source A L'INTERIEUR de la cible (imbrication
    # <id>\<id>\...) au lieu de remplacer son contenu -- on supprime d'abord pour rester
    # idempotent, peu importe l'etat de depart.
    if (Test-Path -LiteralPath $destinationDir) {
        Remove-Item -LiteralPath $destinationDir -Recurse -Force
    }

    Copy-Item -LiteralPath $downloadedDir -Destination $destinationDir -Recurse -Force

    if (-not (Test-Path -LiteralPath $destinationDir)) {
        throw "la copie du mod ${WorkshopId} vers ${destinationDir} a echoue"
    }
}

function Remove-WorkshopMod {
    <#
    .SYNOPSIS
        Supprime le dossier d'un mod Workshop installe. No-op silencieux s'il est deja absent.
    #>
    param(
        [Parameter(Mandatory)]
        $Cfg,

        [Parameter(Mandatory)]
        $ServerCfg,

        [Parameter(Mandatory)]
        [string]$WorkshopId
    )

    $installDir = Get-ServerInstallDir -SteamRoot $Cfg.steamcmd_root -AppId $ServerCfg.appid
    $modDir = Join-Path $installDir "Mods\Workshop\$WorkshopId"

    if (Test-Path -LiteralPath $modDir) {
        Remove-Item -LiteralPath $modDir -Recurse -Force
    }
}

function Invoke-Schtasks {
    <#
    .SYNOPSIS
        Wrapper mockable autour du binaire schtasks.exe (declenche la tache de start).
    .NOTES
        Isole l'appel externe pour rester testable sans Windows reel : les tests mockent
        cette fonction plutot que le binaire schtasks lui-meme.
    #>
    param(
        [Parameter(Mandatory)]
        [string[]]$ArgumentList
    )

    & schtasks.exe @ArgumentList | Out-Null
    return $LASTEXITCODE
}

function Invoke-Taskkill {
    <#
    .SYNOPSIS
        Wrapper mockable autour du binaire taskkill.exe (arret gracieux puis forcage /F).
    .NOTES
        Meme motif que Invoke-Schtasks : isole l'appel externe pour rester testable.
    #>
    param(
        [Parameter(Mandatory)]
        [string[]]$ArgumentList
    )

    & taskkill.exe @ArgumentList | Out-Null
    return $LASTEXITCODE
}

function Stop-GameServer {
    <#
    .SYNOPSIS
        Arrete le serveur de jeu via l'adaptateur declare dans ServerCfg.stop_adapter.
    .NOTES
        "palworld-rcon" : sequence RCON Broadcast -> 60s -> Save -> 10s -> Shutdown 10.
        "generic-graceful" : taskkill SANS /F (laisse le process sauvegarder, ex. Valheim),
        attente 120s max via Wait-Process, puis /F en dernier recours (avec avertissement).
        "generic-force" : taskkill /F direct, sans tentative gracieuse ni attente de 120s --
        reserve aux jeux dont l'arret gracieux echoue systematiquement et immediatement (ex.
        Windrose, confirme le 14/07 : taskkill sans /F erreurait aussitot, faisant quand meme
        attendre les 120s complets de Wait-Process pour rien avant de forcer).
        Dans tous les cas, on attend la fin reelle du process via Wait-Process avant de
        rendre la main -- ne jamais se fier au seul code de retour de la commande d'arret.
    #>
    param(
        [Parameter(Mandatory)]
        $Cfg,

        [Parameter(Mandatory)]
        $ServerCfg,

        [string]$Reason = "Mise_a_jour"
    )

    switch ($ServerCfg.stop_adapter) {
        "palworld-rcon" {
            # Serveur deja mort avant meme de commencer : aucun appel RCON, aucune
            # resolution de mot de passe -- retour immediat. Check fait AVANT toute
            # autre chose (incident 18/07 : serveur mort entre Save et Shutdown a
            # fait remonter une exception non rattrapee jusqu'a Restart-GameServer).
            $procEntry = Get-Process -Name $ServerCfg.process -ErrorAction SilentlyContinue
            if (-not $procEntry) {
                return
            }

            $installDir = Get-ServerInstallDir -SteamRoot $Cfg.steamcmd_root -AppId $ServerCfg.appid
            $settingsIni = Join-Path $installDir "Pal\Saved\Config\WindowsServer\PalWorldSettings.ini"
            $password = Get-PalworldAdminPassword -SettingsIni $settingsIni

            # Annonce + attente SEULEMENT si des joueurs sont (peut-etre) presents :
            # serveur prouve vide (0 strict) = arret direct, ~70s gagnees sur chaque
            # update/restart. Comptage inconnu ($null, ex. RCON ShowPlayers en echec)
            # = prudence, on annonce comme avant. Delai configurable par serveur via
            # stop_warn_seconds (defaut 60).
            $warnSeconds = 60
            if ($ServerCfg.PSObject.Properties.Name -contains "stop_warn_seconds" -and $null -ne $ServerCfg.stop_warn_seconds) {
                $warnSeconds = [int]$ServerCfg.stop_warn_seconds
            }
            $playersCount = $null
            try {
                $playersCount = (Get-PalworldPlayers -Cfg $Cfg -ServerCfg $ServerCfg).Count
            } catch {
                $playersCount = $null
            }

            try {
                if ($playersCount -ne 0) {
                    Invoke-Rcon -RconHost $ServerCfg.rcon.host -Port $ServerCfg.rcon.port -Password $password -Command "Broadcast ${Reason}_dans_${warnSeconds}s" | Out-Null
                    Start-Sleep -Seconds $warnSeconds
                }
                Invoke-Rcon -RconHost $ServerCfg.rcon.host -Port $ServerCfg.rcon.port -Password $password -Command "Save" | Out-Null
                Start-Sleep -Seconds 10
                Invoke-Rcon -RconHost $ServerCfg.rcon.host -Port $ServerCfg.rcon.port -Password $password -Command "Shutdown 10" | Out-Null
            } catch {
                # Serveur mort/mourant en cours de sequence (ex. connexion refusee) :
                # ne pas propager -- fallback taskkill /F sur tout process encore vivant.
                Write-Warning "Stop-GameServer: [$($ServerCfg.name)] sequence RCON echouee ($($_.Exception.Message)) -- fallback taskkill /F"
                $survivors = @(Get-Process -Name $ServerCfg.process -ErrorAction SilentlyContinue | Where-Object { $_ })
                foreach ($proc in $survivors) {
                    Invoke-Taskkill -ArgumentList @("/PID", "$($proc.Id)", "/F")
                }
            }

            $proc = Get-Process -Name $ServerCfg.process -ErrorAction SilentlyContinue
            if ($proc) {
                Wait-Process -Id $proc.Id -Timeout 120 -ErrorAction SilentlyContinue
            }
        }
        "generic-graceful" {
            $procs = @(Get-Process -Name $ServerCfg.process -ErrorAction SilentlyContinue | Where-Object { $_ })
            if ($procs.Count -eq 0) {
                return
            }
            if ($procs.Count -gt 1) {
                Write-Warning "Stop-GameServer: [$($ServerCfg.name)] $($procs.Count) instances detectees simultanement -- arret de toutes"
            }

            foreach ($proc in $procs) {
                Invoke-Taskkill -ArgumentList @("/PID", "$($proc.Id)")
            }

            $exitedGracefully = $true
            foreach ($proc in $procs) {
                try {
                    Wait-Process -Id $proc.Id -Timeout 120 -ErrorAction Stop
                } catch {
                    $exitedGracefully = $false
                }
            }

            if (-not $exitedGracefully) {
                Write-Warning "Stop-GameServer: [$($ServerCfg.name)] arret gracieux non confirme apres 120s pour au moins une instance, forcage taskkill /F"
                foreach ($proc in $procs) {
                    if ((Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) {
                        Invoke-Taskkill -ArgumentList @("/PID", "$($proc.Id)", "/F")
                    }
                }
            }
        }
        "generic-force" {
            $procs = @(Get-Process -Name $ServerCfg.process -ErrorAction SilentlyContinue | Where-Object { $_ })
            if ($procs.Count -eq 0) {
                return
            }
            if ($procs.Count -gt 1) {
                Write-Warning "Stop-GameServer: [$($ServerCfg.name)] $($procs.Count) instances detectees simultanement -- arret de toutes"
            }

            foreach ($proc in $procs) {
                Invoke-Taskkill -ArgumentList @("/PID", "$($proc.Id)", "/F")
            }

            foreach ($proc in $procs) {
                Wait-Process -Id $proc.Id -Timeout 120 -ErrorAction SilentlyContinue
            }
        }
        "rcon-generic" {
            # Source RCON generique : credentials/commandes depuis le REGISTRE (bloc
            # config pousse par le backend), pas de fichier ini specifique au jeu.
            if (-not ($ServerCfg.PSObject.Properties.Name -contains "rcon" -and $ServerCfg.rcon -and $ServerCfg.rcon.password)) {
                throw "Stop-GameServer: rcon-generic sans bloc rcon/password pour '$($ServerCfg.name)'"
            }
            $rc = $ServerCfg.rcon
            $warnSeconds = 60
            if ($ServerCfg.PSObject.Properties.Name -contains "stop_warn_seconds" -and $null -ne $ServerCfg.stop_warn_seconds) {
                $warnSeconds = [int]$ServerCfg.stop_warn_seconds
            }
            # Comptage joueurs : A2S si query_port configure, sinon inconnu ($null) =
            # prudence, on annonce (meme logique que palworld-rcon).
            $playersCount = $null
            if ($ServerCfg.PSObject.Properties.Name -contains "query_port" -and $ServerCfg.query_port) {
                try {
                    $playersCount = Get-A2sPlayerCount -HostName "127.0.0.1" -Port ([int]$ServerCfg.query_port)
                } catch {
                    $playersCount = $null
                }
            }
            $announce = $null
            if ($rc.PSObject.Properties.Name -contains "announce_command" -and $rc.announce_command) {
                $announce = $rc.announce_command
            }
            $shutdownCmd = "shutdown"
            if ($rc.PSObject.Properties.Name -contains "shutdown_command" -and $rc.shutdown_command) {
                $shutdownCmd = $rc.shutdown_command
            }
            try {
                if ($playersCount -ne 0 -and $announce) {
                    $msg = $announce.Replace("{delay}", "$warnSeconds").Replace("{reason}", $Reason)
                    Invoke-Rcon -RconHost $rc.host -Port $rc.port -Password $rc.password -Command $msg | Out-Null
                    Start-Sleep -Seconds $warnSeconds
                }
                Invoke-Rcon -RconHost $rc.host -Port $rc.port -Password $rc.password -Command $shutdownCmd | Out-Null
            } catch {
                # Serveur mort/mourant en cours de sequence (connexion refusee, mot de passe
                # invalide au wizard, timeout) : ne pas propager -- fallback taskkill /F sur
                # tout process encore vivant. Meme resilience que palworld-rcon (incident 18/07 :
                # une exception RCON non rattrapee cassait toute la chaine update/restart).
                Write-Warning "Stop-GameServer: [$($ServerCfg.name)] sequence RCON rcon-generic echouee ($($_.Exception.Message)) -- fallback taskkill /F"
                $survivors = @(Get-Process -Name $ServerCfg.process -ErrorAction SilentlyContinue | Where-Object { $_ })
                foreach ($proc in $survivors) {
                    Invoke-Taskkill -ArgumentList @("/PID", "$($proc.Id)", "/F")
                }
            }

            # Attente de la fin REELLE du process ; forcage /F en dernier recours --
            # jamais se fier au seul retour de la commande RCON.
            $procs = @(Get-Process -Name $ServerCfg.process -ErrorAction SilentlyContinue | Where-Object { $_ })
            $exitedGracefully = $true
            foreach ($proc in $procs) {
                try {
                    Wait-Process -Id $proc.Id -Timeout 120 -ErrorAction Stop
                } catch {
                    $exitedGracefully = $false
                }
            }
            if (-not $exitedGracefully) {
                Write-Warning "Stop-GameServer: [$($ServerCfg.name)] arret rcon-generic non confirme apres 120s, forcage taskkill /F"
                foreach ($proc in $procs) {
                    Invoke-Taskkill -ArgumentList @("/PID", "$($proc.Id)", "/F")
                }
            }
        }
        default {
            throw "Stop-GameServer: stop_adapter inconnu '$($ServerCfg.stop_adapter)' pour '$($ServerCfg.name)'"
        }
    }
}

function Start-GameServer {
    <#
    .SYNOPSIS
        Declenche la tache planifiee de demarrage puis attend que le process soit up.
    .NOTES
        Poll toutes les 2s jusqu'a 60s max ; throw si le process n'apparait jamais --
        verification qui peut echouer (un schtasks "reussi" ne garantit pas un process up).

        PriorityClass forcee explicitement en code (High) une fois le process detecte,
        plutot que de se fier a Settings.Priority de la tache planifiee : verifie
        empiriquement le 19/07/2026 que "schtasks.exe /Run" (utilise ici via
        Invoke-Schtasks) NE RESPECTE PAS la priorite configuree sur la tache
        (le process demarre en PriorityClass Normal quel que soit Settings.Priority),
        contrairement a Start-ScheduledTask (cmdlet) qui la respecte. Comme
        Invoke-Schtasks est necessaire ici (coherent avec Register-ScheduledTask,
        Invoke-RegisterScheduledTask), on ne peut pas compter sur la priorite de la
        tache seule -- il faut la reappliquer sur le process reel une fois lance.
    #>
    param(
        [Parameter(Mandatory)]
        $ServerCfg
    )

    Invoke-Schtasks -ArgumentList @("/Run", "/TN", $ServerCfg.start_task) | Out-Null

    $maxSeconds = 60
    $intervalSeconds = 2
    $elapsed = 0

    while ($elapsed -lt $maxSeconds) {
        $proc = Get-Process -Name $ServerCfg.process -ErrorAction SilentlyContinue
        if ($proc) {
            try {
                $proc.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::High
            } catch {
                Write-Warning "Start-GameServer: [$($ServerCfg.name)] echec elevation PriorityClass ($($_.Exception.Message))"
            }
            return
        }
        Start-Sleep -Seconds $intervalSeconds
        $elapsed += $intervalSeconds
    }

    throw "Start-GameServer: le process '$($ServerCfg.process)' n'est pas monte apres ${maxSeconds}s ('$($ServerCfg.name)')"
}

function Update-GameServer {
    <#
    .SYNOPSIS
        Sequence complete de mise a jour : stop -> steamcmd app_update -> verif buildid -> start.
    .NOTES
        Verification qui peut echouer : le buildid local AVANT/APRES est compare ; un buildid
        inchange alors qu'une MAJ etait attendue est traite comme un echec (ok=$false), meme
        si steamcmd a rendu un exit code 0. En cas d'echec (steamcmd ou buildid inchange), on
        redemarre quand meme l'ancienne version via Start-GameServer plutot que de laisser le
        serveur down.

        Fix 2026-07-13 (F1) : la sortie de l'appel steamcmd est capturee explicitement
        (`2>&1 | Out-String`). Sans cela, la sortie stdout non capturee d'une commande native
        devient la valeur de retour de la FONCTION ENGLOBANTE en PowerShell -- un vrai steamcmd
        emet toujours des centaines de lignes stdout, ce qui aurait transforme le retour de
        Update-GameServer en System.Object[] (lignes + objet melanges) au lieu du
        pscustomobject{ok,detail} attendu. Les dernieres lignes sont incluses dans `detail`
        en cas d'echec, pour le diagnostic.

        Fix 2026-07-13 (F2) : le rollback (redemarrage de l'ancienne version via
        Start-GameServer) est lui-meme protege par un try/catch. Start-GameServer peut throw
        si le process ne remonte pas en 60s -- sans ce garde, cette exception remonterait hors
        de Update-GameServer au lieu de renvoyer proprement {ok=$false, detail}.
    #>
    param(
        [Parameter(Mandatory)]
        $Cfg,

        [Parameter(Mandatory)]
        $ServerCfg
    )

    $manifestPath = Get-ManifestPath -SteamRoot $Cfg.steamcmd_root -AppId $ServerCfg.appid

    Stop-GameServer -Cfg $Cfg -ServerCfg $ServerCfg

    # Backup de la save APRES l'arret (save flushee au shutdown) et AVANT steamcmd :
    # filet de securite contre une MAJ qui empoisonne la save (incident Palworld 15/07).
    # Best-effort : un backup rate ne bloque pas la mise a jour, mais est signale.
    $backupNote = ""
    try {
        if (Get-GameSaveDir -Cfg $Cfg -ServerCfg $ServerCfg) {
            $backupFile = Backup-GameSave -Cfg $Cfg -ServerCfg $ServerCfg -Kind "pre-update"
            $backupNote = " (backup ${backupFile})"
        }
    } catch {
        $backupNote = " (BACKUP DE SAVE ECHOUE: $($_.Exception.Message))"
    }

    $buildBefore = Get-LocalBuildId -ManifestPath $manifestPath

    # PAS de +force_install_dir (incident 14-15/07) : la racine steamcmd est deja la
    # bibliotheque par defaut (fichiers sous steamapps\common\<installdir>, manifest sous
    # steamapps\). Le forcer sur install_dir deplace le manifest (buildid jamais vu changer),
    # le forcer sur steamcmd_root deplace les FICHIERS DU JEU a la racine (exit code 8
    # "Missing game files", et Windrose installe en double dans C:\steam le 14/07).
    $steamArgs = @(
        "+login", "anonymous",
        "+app_update", "$($ServerCfg.appid)", "validate",
        "+quit"
    )
    $steamOutput = & $Cfg.steamcmd @steamArgs 2>&1 | Out-String
    $steamExitCode = $LASTEXITCODE

    if ($steamExitCode -ne 0) {
        $tail = (($steamOutput -split "`r?`n" | Where-Object { $_ -ne "" } | Select-Object -Last 5) -join " / ")
        $rollbackDetail = ""
        try {
            Start-GameServer -ServerCfg $ServerCfg
        } catch {
            $rollbackDetail = " ET le redemarrage de secours a aussi echoue : $($_.Exception.Message)"
        }
        return [pscustomobject]@{
            ok     = $false
            detail = "steamcmd a echoue (exit code ${steamExitCode}): ${tail}${rollbackDetail}"
        }
    }

    $buildAfter = Get-LocalBuildId -ManifestPath $manifestPath

    if ($buildAfter -eq $buildBefore) {
        $rollbackDetail = ""
        try {
            Start-GameServer -ServerCfg $ServerCfg
        } catch {
            $rollbackDetail = " ET le redemarrage de secours a aussi echoue : $($_.Exception.Message)"
        }
        return [pscustomobject]@{
            ok     = $false
            detail = "buildid inchange apres steamcmd (toujours ${buildBefore}) -- mise a jour non appliquee${rollbackDetail}${backupNote}"
        }
    }

    Start-GameServer -ServerCfg $ServerCfg

    return [pscustomobject]@{
        ok     = $true
        detail = "mise a jour reussie : ${buildBefore} -> ${buildAfter}${backupNote}"
    }
}

function Restart-GameServer {
    <#
    .SYNOPSIS
        Stop -> backup de la save (best-effort, si save_dir configure) -> start.
        Retourne une note de backup ("" ou " (backup xxx.zip)") a inclure dans le
        detail de l'ordre.
    #>
    param(
        [Parameter(Mandatory)]
        $Cfg,

        [Parameter(Mandatory)]
        $ServerCfg
    )

    Stop-GameServer -Cfg $Cfg -ServerCfg $ServerCfg -Reason "Redemarrage"

    $backupNote = ""
    try {
        if (Get-GameSaveDir -Cfg $Cfg -ServerCfg $ServerCfg) {
            $backupFile = Backup-GameSave -Cfg $Cfg -ServerCfg $ServerCfg -Kind "pre-restart"
            $backupNote = " (backup ${backupFile})"
        }
    } catch {
        $backupNote = " (BACKUP DE SAVE ECHOUE: $($_.Exception.Message))"
    }

    Start-GameServer -ServerCfg $ServerCfg
    return $backupNote
}
