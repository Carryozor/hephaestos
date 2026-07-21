Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot/../hephaestos-lib.ps1"

    # Stream de test duplex : lecture depuis un buffer de reponses "en boite" (simule
    # les paquets renvoyes par un serveur RCON), ecriture capturee dans un MemoryStream
    # separe pour verifier ce que Invoke-Rcon envoie reellement sur le fil.
    Add-Type -TypeDefinition @"
using System;
using System.IO;

public class RconTestStream : Stream
{
    private MemoryStream _readBuf;
    public MemoryStream WriteBuf;

    public RconTestStream(byte[] canned)
    {
        _readBuf = new MemoryStream(canned);
        WriteBuf = new MemoryStream();
    }

    public override bool CanRead { get { return true; } }
    public override bool CanWrite { get { return true; } }
    public override bool CanSeek { get { return false; } }
    public override long Length { get { throw new NotSupportedException(); } }
    public override long Position { get { throw new NotSupportedException(); } set { throw new NotSupportedException(); } }
    public override void Flush() { }
    public override int Read(byte[] buffer, int offset, int count) { return _readBuf.Read(buffer, offset, count); }
    public override void Write(byte[] buffer, int offset, int count) { WriteBuf.Write(buffer, offset, count); }
    public override long Seek(long offset, SeekOrigin origin) { throw new NotSupportedException(); }
    public override void SetLength(long value) { throw new NotSupportedException(); }
}
"@ -ErrorAction SilentlyContinue
}

Describe "New-RconPacket" {
    It "construit le paquet de reference valide contre le vrai serveur Palworld le 13/07 (id=1, type=3, body='pwd')" {
        # Reference octet par octet, calculee a la main d'apres le protocole Source RCON verifie
        # contre le vrai serveur 203.0.113.10:25575 (probe Python du 13/07) :
        # length(int32 LE) = 13 (id[4] + type[4] + body[3] + 2 null terminators)
        # id(int32 LE) = 1 ; type(int32 LE) = 3 ; body = "pwd" (ASCII) ; puis 2 null bytes.
        $expected = [byte[]](13,0,0,0, 1,0,0,0, 3,0,0,0, 0x70,0x77,0x64, 0,0)

        $actual = New-RconPacket -Id 1 -Type 3 -Body "pwd"

        # longueur totale = 14 (length+id+type+2 null) + 3 (body) = 17
        $actual.Length | Should -Be 17
        ($actual -join ',') | Should -Be ($expected -join ',')
    }

    It "place l'id et le type aux bons offsets pour un id/type/body differents" {
        $actual = New-RconPacket -Id 42 -Type 2 -Body "ShowPlayers"

        [BitConverter]::ToInt32($actual, 0) | Should -Be (4 + 4 + 11 + 1 + 1)
        [BitConverter]::ToInt32($actual, 4) | Should -Be 42
        [BitConverter]::ToInt32($actual, 8) | Should -Be 2
        $actual[$actual.Length - 1] | Should -Be 0
        $actual[$actual.Length - 2] | Should -Be 0
    }

    It "gere un body vide (paquet EXEC/AUTH sans corps)" {
        $actual = New-RconPacket -Id 7 -Type 3 -Body ""

        $actual.Length | Should -Be 14
        [BitConverter]::ToInt32($actual, 0) | Should -Be 10
    }
}

Describe "Read-RconPacket" {
    It "parse une reponse AUTH refusee (id=-1, type=2, body vide) -- verifie le 13/07 contre le vrai serveur" {
        # Paquet de reference : length=10, id=-1, type=2, body="", 2 null bytes.
        $bytes = [byte[]](10,0,0,0, 0xFF,0xFF,0xFF,0xFF, 2,0,0,0, 0,0)
        $stream = New-Object System.IO.MemoryStream(,$bytes)

        $result = Read-RconPacket -Stream $stream

        $result.Id | Should -Be -1
        $result.Type | Should -Be 2
        $result.Body | Should -Be ""
    }

    It "parse une reponse normale avec un corps non vide" {
        $bodyBytes = [Text.Encoding]::ASCII.GetBytes("hello")
        $length = 4 + 4 + $bodyBytes.Length + 1 + 1
        $lengthBytes = [BitConverter]::GetBytes([int32]$length)
        $idBytes = [BitConverter]::GetBytes([int32]5)
        $typeBytes = [BitConverter]::GetBytes([int32]0)
        $bytes = $lengthBytes + $idBytes + $typeBytes + $bodyBytes + [byte[]](0,0)
        $stream = New-Object System.IO.MemoryStream(,$bytes)

        $result = Read-RconPacket -Stream $stream

        $result.Id | Should -Be 5
        $result.Type | Should -Be 0
        $result.Body | Should -Be "hello"
    }
}

Describe "Invoke-Rcon" {
    It "leve 'RCON auth refused' quand la reponse AUTH a id=-1 (paquet de reference AUTH-fail)" {
        # Paquet de reponse AUTH refuse : length=10, id=-1, type=2, body vide -- format
        # verifie le 13/07 contre le vrai serveur Palworld sur mot de passe errone.
        $authFailBytes = [byte[]](10,0,0,0, 0xFF,0xFF,0xFF,0xFF, 2,0,0,0, 0,0)
        $stream = New-Object RconTestStream(,$authFailBytes)

        { Invoke-Rcon -RconHost "unused" -Port 0 -Password "wrong" -Command "ShowPlayers" -Stream $stream } |
            Should -Throw "*RCON auth refused*"

        # Verifie que le paquet AUTH envoye correspond bien au mot de passe fourni
        # (id=1, type=3, body="wrong") -- discrimine un bug qui enverrait un mauvais body/type.
        $sent = $stream.WriteBuf.ToArray()
        $expectedAuth = New-RconPacket -Id 1 -Type 3 -Body "wrong"
        ($sent -join ',') | Should -Be ($expectedAuth -join ',')
    }

    It "authentifie puis execute la commande et retourne le corps de la reponse EXEC" {
        # AUTH ok (id=1, type=2, body vide) suivi de la reponse EXEC (id=2, type=0, body="pong").
        $authOkBytes = [byte[]](10,0,0,0, 1,0,0,0, 2,0,0,0, 0,0)
        $execBody = [Text.Encoding]::ASCII.GetBytes("pong")
        $execLength = 4 + 4 + $execBody.Length + 1 + 1
        $execBytes = [BitConverter]::GetBytes([int32]$execLength) + [BitConverter]::GetBytes([int32]2) + [BitConverter]::GetBytes([int32]0) + $execBody + [byte[]](0,0)
        $stream = New-Object RconTestStream(,($authOkBytes + $execBytes))

        $result = Invoke-Rcon -RconHost "unused" -Port 0 -Password "good" -Command "ShowPlayers" -Stream $stream

        $result | Should -Be "pong"

        # Le second paquet envoye doit etre l'EXEC avec la commande demandee.
        $sent = $stream.WriteBuf.ToArray()
        $expectedAuth = New-RconPacket -Id 1 -Type 3 -Body "good"
        $expectedExec = New-RconPacket -Id 2 -Type 2 -Body "ShowPlayers"
        $expectedSent = $expectedAuth + $expectedExec
        ($sent -join ',') | Should -Be ($expectedSent -join ',')
    }

    It "ignore un paquet vide type=0 intercale avant l'AUTH_RESPONSE type=2 (auth reussie)" {
        # Certains serveurs RCON Source envoient un SERVERDATA_RESPONSE_VALUE (type=0, body
        # vide) juste apres une auth acceptee, AVANT le vrai SERVERDATA_AUTH_RESPONSE (type=2).
        # Invoke-Rcon doit ignorer ce paquet vide et attendre le type=2 pour lire le id reel.
        $emptyPacket = New-RconPacket -Id 1 -Type 0 -Body ""
        $authOkPacket = New-RconPacket -Id 1 -Type 2 -Body ""
        $execBody = [Text.Encoding]::ASCII.GetBytes("pong")
        $execLength = 4 + 4 + $execBody.Length + 1 + 1
        $execBytes = [BitConverter]::GetBytes([int32]$execLength) + [BitConverter]::GetBytes([int32]2) + [BitConverter]::GetBytes([int32]0) + $execBody + [byte[]](0,0)

        $stream = New-Object RconTestStream(,($emptyPacket + $authOkPacket + $execBytes))

        $result = Invoke-Rcon -RconHost "unused" -Port 0 -Password "good" -Command "ShowPlayers" -Stream $stream

        $result | Should -Be "pong"
    }

    It "ignore un paquet vide type=0 intercale avant l'AUTH_RESPONSE type=2 (auth refusee)" {
        $emptyPacket = New-RconPacket -Id 1 -Type 0 -Body ""
        $authFailPacket = New-RconPacket -Id -1 -Type 2 -Body ""

        $stream = New-Object RconTestStream(,($emptyPacket + $authFailPacket))

        { Invoke-Rcon -RconHost "unused" -Port 0 -Password "wrong" -Command "ShowPlayers" -Stream $stream } |
            Should -Throw "*RCON auth refused*"
    }

    It "leve une exception explicite si aucun paquet type=2 n'arrive avant la limite de lecture" {
        # 3 paquets type=0 successifs, jamais de type=2 : flux corrompu / serveur qui ne repond
        # jamais l'AUTH_RESPONSE attendue -- ne doit pas boucler indefiniment.
        $p1 = New-RconPacket -Id 1 -Type 0 -Body ""
        $p2 = New-RconPacket -Id 1 -Type 0 -Body ""
        $p3 = New-RconPacket -Id 1 -Type 0 -Body ""

        $stream = New-Object RconTestStream(,($p1 + $p2 + $p3))

        { Invoke-Rcon -RconHost "unused" -Port 0 -Password "wrong" -Command "ShowPlayers" -Stream $stream } |
            Should -Throw "*AUTH_RESPONSE*"
    }
}

Describe "New-RconClient" {
    It "configure SendTimeout et ReceiveTimeout a la valeur demandee, sans ouvrir de connexion reseau" {
        $client = New-RconClient -TimeoutMs 5000
        try {
            $client.SendTimeout | Should -Be 5000
            $client.ReceiveTimeout | Should -Be 5000
        } finally {
            $client.Close()
        }
    }

    It "utilise 5000ms par defaut si TimeoutMs n'est pas fourni" {
        $client = New-RconClient
        try {
            $client.SendTimeout | Should -Be 5000
            $client.ReceiveTimeout | Should -Be 5000
        } finally {
            $client.Close()
        }
    }

    It "respecte un TimeoutMs personnalise" {
        $client = New-RconClient -TimeoutMs 2500
        try {
            $client.SendTimeout | Should -Be 2500
            $client.ReceiveTimeout | Should -Be 2500
        } finally {
            $client.Close()
        }
    }
}

Describe "Get-PalworldAdminPassword" {
    It "extrait AdminPassword d'un fichier ini d'exemple" {
        $ini = Join-Path $TestDrive "PalWorldSettings.ini"
        @'
[/Script/Pal.PalGameWorldSettings]
OptionSettings=(Difficulty=None,DayTimeSpeedRate=1.000000,AdminPassword="s3cr3t-pwd",bIsMultiplay=True)
'@ | Set-Content -LiteralPath $ini -Encoding UTF8

        Get-PalworldAdminPassword -SettingsIni $ini | Should -Be "s3cr3t-pwd"
    }

    It "leve une exception si AdminPassword est absent du fichier" {
        $ini = Join-Path $TestDrive "PalWorldSettings-noadmin.ini"
        @'
[/Script/Pal.PalGameWorldSettings]
OptionSettings=(Difficulty=None,DayTimeSpeedRate=1.000000,bIsMultiplay=True)
'@ | Set-Content -LiteralPath $ini -Encoding UTF8

        { Get-PalworldAdminPassword -SettingsIni $ini } | Should -Throw
    }

    It "leve une exception si le fichier est introuvable" {
        { Get-PalworldAdminPassword -SettingsIni (Join-Path $TestDrive "nope.ini") } | Should -Throw
    }
}

Describe "Get-PalworldPlayers" {
    BeforeAll {
        # Racine SteamCMD de test : manifest reel (installdir) + ini au chemin DERIVE
        # (steamcmd_root/steamapps/common/<installdir>/Pal/Saved/Config/WindowsServer/...),
        # pour verifier que le settings_ini n'est plus une valeur codee en dur du fixture
        # mais bien resolu dynamiquement via Get-ServerInstallDir.
        $script:steamRootCount = Join-Path $TestDrive "steam-count"
        $script:manifestDirCount = Join-Path $script:steamRootCount "steamapps"
        New-Item -ItemType Directory -Path $script:manifestDirCount -Force | Out-Null
        @'
"AppState"
{
	"appid"		"2394010"
	"installdir"		"PalServer"
	"buildid"		"100"
}
'@ | Set-Content -LiteralPath (Join-Path $script:manifestDirCount "appmanifest_2394010.acf") -Encoding UTF8

        $script:iniDirCount = Join-Path $script:steamRootCount "steamapps\common\PalServer\Pal\Saved\Config\WindowsServer"
        New-Item -ItemType Directory -Path $script:iniDirCount -Force | Out-Null
        @'
[/Script/Pal.PalGameWorldSettings]
OptionSettings=(AdminPassword="pwd123")
'@ | Set-Content -LiteralPath (Join-Path $script:iniDirCount "PalWorldSettings.ini") -Encoding UTF8

        $script:cfgCount = [pscustomobject]@{ steamcmd_root = $script:steamRootCount }
        $script:serverCfg = [pscustomobject]@{
            appid = 2394010
            rcon  = [pscustomobject]@{
                host = "127.0.0.1"
                port = 25575
            }
        }
    }

    It "retourne 1 joueur pour une ligne CSV en plus de l'en-tete, avec ses infos" {
        Mock Invoke-Rcon { return "name,playeruid,steamid`nAlice,123,76561197960287930" }

        $result = Get-PalworldPlayers -Cfg $script:cfgCount -ServerCfg $script:serverCfg

        $result.Count | Should -Be 1
        $result.Players[0].name | Should -Be "Alice"
        $result.Players[0].playeruid | Should -Be "123"
        $result.Players[0].steamid | Should -Be "76561197960287930"
    }

    It "retourne 0 joueur (tableau vide, pas null) quand seul l'en-tete est present" {
        Mock Invoke-Rcon { return "name,playeruid,steamid" }

        $result = Get-PalworldPlayers -Cfg $script:cfgCount -ServerCfg $script:serverCfg

        $result.Count | Should -Be 0
        $result.Players.Count | Should -Be 0
    }

    It "extrait plusieurs joueurs distincts depuis plusieurs lignes CSV" {
        Mock Invoke-Rcon { return "name,playeruid,steamid`nAlice,123,76561197960287930`nBob,456,76561197960287931" }

        $result = Get-PalworldPlayers -Cfg $script:cfgCount -ServerCfg $script:serverCfg

        $result.Count | Should -Be 2
        $result.Players[1].name | Should -Be "Bob"
        $result.Players[1].steamid | Should -Be "76561197960287931"
    }

    It "appelle Invoke-Rcon avec le mot de passe lu dans le settings_ini DERIVE et la commande ShowPlayers" {
        Mock Invoke-Rcon { return "name,playeruid,steamid" }

        Get-PalworldPlayers -Cfg $script:cfgCount -ServerCfg $script:serverCfg | Out-Null

        Should -Invoke Invoke-Rcon -Times 1 -ParameterFilter {
            $RconHost -eq "127.0.0.1" -and $Port -eq 25575 -and $Password -eq "pwd123" -and $Command -eq "ShowPlayers"
        }
    }
}

Describe "Get-ServerRconInfo" {
    BeforeAll {
        $script:steamRootInfo = Join-Path $TestDrive "steam-info"
        $manifestDirInfo = Join-Path $script:steamRootInfo "steamapps"
        New-Item -ItemType Directory -Path $manifestDirInfo -Force | Out-Null
        @'
"AppState"
{
	"appid"		"2394010"
	"installdir"		"PalServer"
	"buildid"		"100"
}
'@ | Set-Content -LiteralPath (Join-Path $manifestDirInfo "appmanifest_2394010.acf") -Encoding UTF8

        $iniDirInfo = Join-Path $script:steamRootInfo "steamapps\common\PalServer\Pal\Saved\Config\WindowsServer"
        New-Item -ItemType Directory -Path $iniDirInfo -Force | Out-Null
        @'
[/Script/Pal.PalGameWorldSettings]
OptionSettings=(AdminPassword="pwd123")
'@ | Set-Content -LiteralPath (Join-Path $iniDirInfo "PalWorldSettings.ini") -Encoding UTF8

        $script:cfgInfo = [pscustomobject]@{ steamcmd_root = $script:steamRootInfo }
        $script:serverCfgWithRcon = [pscustomobject]@{
            appid = 2394010
            rcon  = [pscustomobject]@{ host = "127.0.0.1"; port = 25575 }
        }
        $script:serverCfgNoRcon = [pscustomobject]@{ appid = 4129620 }
    }

    It "retourne le corps de la commande RCON Info pour un serveur Palworld" {
        Mock Invoke-Rcon { return "Welcome to Pal Server[Version:0.1.2] MyWorld" }

        $result = Get-ServerRconInfo -Cfg $script:cfgInfo -ServerCfg $script:serverCfgWithRcon

        $result | Should -Be "Welcome to Pal Server[Version:0.1.2] MyWorld"
        Should -Invoke Invoke-Rcon -Times 1 -ParameterFilter {
            $RconHost -eq "127.0.0.1" -and $Port -eq 25575 -and $Password -eq "pwd123" -and $Command -eq "Info"
        }
    }

    It "retourne `$null sans appeler Invoke-Rcon pour un serveur sans config rcon" {
        Mock Invoke-Rcon { return "ne devrait jamais etre appele" }

        $result = Get-ServerRconInfo -Cfg $script:cfgInfo -ServerCfg $script:serverCfgNoRcon

        $result | Should -Be $null
        Should -Invoke Invoke-Rcon -Times 0
    }
}

Describe "Stop-GameServer rcon-generic" {
    BeforeEach {
        $script:cfg = [pscustomobject]@{ steamcmd_root = $TestDrive }
        $script:serverCfg = [pscustomobject]@{
            name = "vrising"; appid = 1829350; process = "VRisingServer"
            stop_adapter = "rcon-generic"; stop_warn_seconds = 5
            rcon = [pscustomobject]@{ host = "127.0.0.1"; port = 25580; password = "s3cret"
                shutdown_command = "shutdown"
                announce_command = 'announce Arret ({reason}) dans {delay}s' }
        }
        Mock Start-Sleep { }
        Mock Get-Process { $null }   # process deja down apres shutdown
        Mock Wait-Process { }
        Mock Invoke-Taskkill { 0 }
    }

    It "annonce (placeholders substitues) puis envoie shutdown_command avec le password du registre" {
        Mock Invoke-Rcon { "" }
        Stop-GameServer -Cfg $script:cfg -ServerCfg $script:serverCfg -Reason "Maj"
        Should -Invoke Invoke-Rcon -Times 1 -ParameterFilter {
            $Command -eq "announce Arret (Maj) dans 5s" -and $Password -eq "s3cret" -and $Port -eq 25580
        }
        Should -Invoke Invoke-Rcon -Times 1 -ParameterFilter { $Command -eq "shutdown" }
    }

    It "0 joueur prouve par A2S : pas d'annonce, shutdown direct" {
        $script:serverCfg | Add-Member -NotePropertyName query_port -NotePropertyValue 27016
        Mock Get-A2sPlayerCount { 0 }
        Mock Get-Process { [pscustomobject]@{ Id = 42 } } -ParameterFilter { $Name -eq "VRisingServer" }
        Mock Invoke-Rcon { "" }
        Stop-GameServer -Cfg $script:cfg -ServerCfg $script:serverCfg -Reason "Maj"
        Should -Invoke Invoke-Rcon -Times 0 -ParameterFilter { $Command -like "announce*" }
        Should -Invoke Invoke-Rcon -Times 1 -ParameterFilter { $Command -eq "shutdown" }
    }

    It "sans shutdown_command, envoie 'shutdown' par defaut" {
        $script:serverCfg.rcon = [pscustomobject]@{ host = "127.0.0.1"; port = 25580; password = "s3cret" }
        Mock Invoke-Rcon { "" }
        Stop-GameServer -Cfg $script:cfg -ServerCfg $script:serverCfg
        Should -Invoke Invoke-Rcon -Times 1 -ParameterFilter { $Command -eq "shutdown" }
    }

    It "process toujours up apres l'attente -> taskkill /F" {
        Mock Invoke-Rcon { "" }
        Mock Get-Process { [pscustomobject]@{ Id = 42 } } -ParameterFilter { $Name -eq "VRisingServer" }
        Mock Wait-Process { throw "timeout" }
        Stop-GameServer -Cfg $script:cfg -ServerCfg $script:serverCfg
        Should -Invoke Invoke-Taskkill -Times 1 -ParameterFilter { ($ArgumentList -join " ") -match "/F" }
    }

    It "Invoke-Rcon leve une exception (auth refusee/connexion) -> fallback taskkill /F, pas de throw sortant" {
        # Meme resilience que palworld-rcon (incident 18/07) : un RCON injoignable ou un
        # mot de passe invalide au wizard ne doit jamais casser la chaine update/restart.
        Mock Invoke-Rcon { throw "RCON auth refused" }
        Mock Get-Process { [pscustomobject]@{ Id = 42 } } -ParameterFilter { $Name -eq "VRisingServer" }
        { Stop-GameServer -Cfg $script:cfg -ServerCfg $script:serverCfg } | Should -Not -Throw
        Should -Invoke Invoke-Taskkill -Times 1 -ParameterFilter {
            ($ArgumentList -join " ") -match "/F" -and ($ArgumentList -join " ") -match "42"
        }
    }

    It "bloc rcon absent -> throw explicite" {
        $script:serverCfg.rcon = $null
        { Stop-GameServer -Cfg $script:cfg -ServerCfg $script:serverCfg } | Should -Throw "*rcon*"
    }
}
