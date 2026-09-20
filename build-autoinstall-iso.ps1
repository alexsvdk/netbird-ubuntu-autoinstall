<#
.SYNOPSIS
    Builds a bootable unattended Ubuntu Server ISO with NetBird integration for Windows.
    Compatible with Windows PowerShell 5.1+ and PowerShell 7+.
#>

[CmdletBinding()]
param(
    [string]$Arch = $env:ARCH,
    [string]$UbuntuVersion = $env:UBUNTU_VERSION,
    [string]$OutputIso = $env:OUTPUT_ISO,
    [string]$IsoMirror = $env:ISO_MIRROR,
    [string]$IsoUrl = $env:ISO_URL
)

$ErrorActionPreference = "Stop"

$WorkDir = $PSScriptRoot
if (-not $WorkDir) {
    $WorkDir = (Get-Location).Path
}

# Windows PowerShell 5.1 otherwise writes a UTF-8 BOM. A BOM before a shell
# shebang is parsed as a command by bash and makes the build continue incorrectly.
$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)

# Space-delimited / hash set of keys that appeared in .env
$EnvFileKeys = @{}

function Load-EnvFile([string]$path) {
    if (-not (Test-Path $path)) { return }
    Write-Host "Loading environment from $path"
    $lines = [System.IO.File]::ReadAllLines($path, $Utf8NoBom)
    foreach ($rawLine in $lines) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#")) { continue }

        if ($line -match '^export\s+(.*)$') {
            $line = $matches[1].Trim()
        }

        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $key = $matches[1].Trim()
            $val = $matches[2].Trim()

            # Strip matching quotes
            if (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'"))) {
                if ($val.Length -ge 2) {
                    $val = $val.Substring(1, $val.Length - 2)
                }
            }

            $script:EnvFileKeys[$key] = $true
            [System.Environment]::SetEnvironmentVariable($key, $val, "Process")
        }
    }
}

function Test-Configured([string]$name) {
    $val = [System.Environment]::GetEnvironmentVariable($name, "Process")
    if ([string]::IsNullOrWhiteSpace($val)) { return $false }
    if ($name -eq "HOSTNAME") {
        return ($script:EnvFileKeys.ContainsKey("HOSTNAME") -or (-not [string]::IsNullOrWhiteSpace($env:TARGET_HOSTNAME)))
    }
    return $true
}

Load-EnvFile (Join-Path $WorkDir ".env")

if (-not [string]::IsNullOrWhiteSpace($env:TARGET_HOSTNAME)) {
    $env:HOSTNAME = $env:TARGET_HOSTNAME
}

function Assert-Tool([string]$cmdName) {
    $found = Get-Command $cmdName -ErrorAction SilentlyContinue
    if (-not $found) {
        Write-Error "Error: '$cmdName' is required."
        exit 1
    }
}

Assert-Tool "docker"

$CurlCmd = Get-Command "curl.exe" -ErrorAction SilentlyContinue
if (-not $CurlCmd) {
    $CurlCmd = Get-Command "curl" -ErrorAction SilentlyContinue
}

Write-Host "This creates a FULLY UNATTENDED installer."
Write-Host "It will erase the largest non-USB installation disk."
Write-Host ""

# CPU architecture
if (-not (Test-Configured "ARCH") -and [string]::IsNullOrWhiteSpace($Arch)) {
    $Arch = Read-Host "CPU architecture (amd64/arm64) [amd64]"
    if ([string]::IsNullOrWhiteSpace($Arch)) { $Arch = "amd64" }
} elseif (-not [string]::IsNullOrWhiteSpace($env:ARCH)) {
    $Arch = $env:ARCH
} else {
    if ([string]::IsNullOrWhiteSpace($Arch)) { $Arch = "amd64" }
}

switch -Regex ($Arch.ToLower()) {
    '^(amd64|x86_64|x64)$' { $Arch = 'amd64' }
    '^(arm64|aarch64|arm)$' { $Arch = 'arm64' }
    default {
        Write-Error "Error: unsupported ARCH '$Arch'. Use amd64 (x86_64) or arm64 (aarch64)."
        exit 1
    }
}
$env:ARCH = $Arch

# Ubuntu version & series
if ([string]::IsNullOrWhiteSpace($UbuntuVersion)) {
    $UbuntuVersion = if ($env:UBUNTU_VERSION) { $env:UBUNTU_VERSION } else { "24.04.4" }
}
$env:UBUNTU_VERSION = $UbuntuVersion

if ($UbuntuVersion -match '^([0-9]+\.[0-9]+)') {
    $UbuntuSeries = $matches[1]
} else {
    $UbuntuSeries = $UbuntuVersion
}

$IsoName = "ubuntu-${UbuntuVersion}-live-server-${Arch}.iso"
if ([string]::IsNullOrWhiteSpace($OutputIso)) {
    $OutputIso = if ($env:OUTPUT_ISO) { $env:OUTPUT_ISO } else { "ubuntu-${UbuntuVersion}-autoinstall-${Arch}.iso" }
}
$env:OUTPUT_ISO = $OutputIso

# Resolve the host output path before passing it to Docker. An absolute Windows
# path (for example G:\Samovar\ubuntu-samovar-amd64.iso) must not be appended
# to the project directory or passed verbatim as a Linux path inside the
# container.
$OutputIsoIsAbsolute = [System.IO.Path]::IsPathRooted($OutputIso)
if ($OutputIsoIsAbsolute) {
    try {
        $OutputIsoPath = [System.IO.Path]::GetFullPath($OutputIso)
    } catch {
        Write-Error "Error: invalid OUTPUT_ISO path '$OutputIso'."
        exit 1
    }

    $OutputIsoName = [System.IO.Path]::GetFileName($OutputIsoPath)
    $OutputIsoDir = [System.IO.Path]::GetDirectoryName($OutputIsoPath)
    if ([string]::IsNullOrWhiteSpace($OutputIsoName) -or [string]::IsNullOrWhiteSpace($OutputIsoDir)) {
        Write-Error "Error: OUTPUT_ISO must name an ISO file."
        exit 1
    }
    if (-not (Test-Path -LiteralPath $OutputIsoDir)) {
        New-Item -ItemType Directory -Path $OutputIsoDir -Force | Out-Null
    }
    $ContainerOutputIsoPath = "/output/$OutputIsoName"
} else {
    $OutputIsoPath = Join-Path $WorkDir $OutputIso
    $OutputIsoName = [System.IO.Path]::GetFileName($OutputIsoPath)
    $OutputIsoDir = [System.IO.Path]::GetDirectoryName($OutputIsoPath)
    $ContainerOutputIsoPath = "/work/$OutputIso"
}

# Curated mirror candidate URLs
function Get-IsoCandidateUrls([string]$targetArch, [string]$series, [string]$iso) {
    if ($targetArch -eq "amd64") {
        return @(
            "https://releases.ubuntu.com/${series}/${iso}",
            "https://mirror.yandex.ru/ubuntu-releases/${series}/${iso}",
            "https://ftp.halifax.rwth-aachen.de/ubuntu-releases/${series}/${iso}",
            "https://ftp.uni-stuttgart.de/ubuntu-releases/${series}/${iso}",
            "https://mirror.nl.leaseweb.net/ubuntu-releases/${series}/${iso}",
            "https://mirror.rackspace.com/ubuntu-releases/${series}/${iso}",
            "https://mirror.csclub.uwaterloo.ca/ubuntu-releases/${series}/${iso}",
            "https://mirrors.ocf.berkeley.edu/ubuntu-releases/${series}/${iso}",
            "https://mirror.math.princeton.edu/pub/ubuntu-iso/${series}/${iso}",
            "https://mirror.aarnet.edu.au/pub/ubuntu/releases/${series}/${iso}",
            "https://ftp.jaist.ac.jp/pub/Linux/ubuntu-releases/${series}/${iso}",
            "https://ftp.riken.jp/Linux/ubuntu-releases/${series}/${iso}",
            "https://mirror.nju.edu.cn/ubuntu-releases/${series}/${iso}",
            "https://mirrors.aliyun.com/ubuntu-releases/${series}/${iso}"
        )
    } else {
        return @(
            "https://cdimage.ubuntu.com/releases/${series}/release/${iso}",
            "https://ftp.jaist.ac.jp/pub/Linux/ubuntu-cdimage/releases/${series}/release/${iso}"
        )
    }
}

function Get-OfficialIsoUrl([string]$targetArch, [string]$series, [string]$iso) {
    if ($targetArch -eq "amd64") {
        return "https://releases.ubuntu.com/${series}/${iso}"
    } else {
        return "https://cdimage.ubuntu.com/releases/${series}/release/${iso}"
    }
}

function Format-Speed([double]$bytesPerSec) {
    if ($bytesPerSec -ge 1048576) {
        return ("{0,5:N1} MB/s" -f ($bytesPerSec / 1048576))
    } elseif ($bytesPerSec -ge 1024) {
        return ("{0,5:N0} KB/s" -f ($bytesPerSec / 1024))
    } else {
        return ("{0,5:N0} B/s" -f $bytesPerSec)
    }
}

function Probe-IsoMirror([string]$url) {
    if ($script:CurlCmd) {
        try {
            $result = & $script:CurlCmd.Source -fsSL --range 0-2097151 --connect-timeout 3 --max-time 12 -o NUL -w "%{http_code} %{speed_download}" $url 2>$null
            if ($result -match '^(\d+)\s+([\d\.]+)') {
                $code = [int]$matches[1]
                $speed = [double]$matches[2]
                if (($code -eq 200 -or $code -eq 206) -and $speed -gt 0) {
                    return $speed
                }
            }
        } catch {}
    }
    # Fallback to .NET HttpWebRequest
    try {
        $req = [System.Net.HttpWebRequest]::Create($url)
        $req.Method = "GET"
        $req.Timeout = 12000
        $req.AddRange(0, 2097151)
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $resp = $req.GetResponse()
        $stream = $resp.GetResponseStream()
        $buf = New-Object byte[] 32768
        $total = 0
        while ($true) {
            $read = $stream.Read($buf, 0, $buf.Length)
            if ($read -le 0) { break }
            $total += $read
            if ($sw.ElapsedMilliseconds -ge 12000) { break }
        }
        $stream.Close()
        $resp.Close()
        $sw.Stop()
        $secs = [Math]::Max($sw.Elapsed.TotalSeconds, 0.001)
        return [double]($total / $secs)
    } catch {
        return 0.0
    }
}

function Resolve-IsoUrl([string]$mirrorPref, [string]$targetArch, [string]$series, [string]$iso) {
    if (-not [string]::IsNullOrWhiteSpace($env:ISO_URL)) {
        return $env:ISO_URL
    }
    if ($mirrorPref -eq "default") {
        return (Get-OfficialIsoUrl $targetArch $series $iso)
    }
    if ($mirrorPref -eq "auto" -or [string]::IsNullOrWhiteSpace($mirrorPref)) {
        Write-Host "Probing Ubuntu ISO mirrors for best download speed (${targetArch})..."
        $candidates = Get-IsoCandidateUrls $targetArch $series $iso
        $bestSpeed = 0.0
        $bestUrl = ""
        Write-Host "ISO mirror probe results:"
        foreach ($cand in $candidates) {
            $speed = Probe-IsoMirror $cand
            if ($speed -gt 0) {
                Write-Host ("  {0}  {1}" -f (Format-Speed $speed), $cand)
            } else {
                Write-Host ("  fail        {0}" -f $cand)
            }
            if ($speed -gt $bestSpeed) {
                $bestSpeed = $speed
                $bestUrl = $cand
            }
        }
        if ($bestUrl -and $bestSpeed -gt 0) {
            Write-Host ("Selected: {0}  {1}" -f (Format-Speed $bestSpeed), $bestUrl)
            return $bestUrl
        }
        $official = Get-OfficialIsoUrl $targetArch $series $iso
        Write-Host "Mirror probe found no working host; using official: $official"
        return $official
    }
    if ($mirrorPref.EndsWith(".iso")) {
        return $mirrorPref
    }
    $base = $mirrorPref.TrimEnd('/')
    if ($targetArch -eq "amd64") {
        return "${base}/${series}/${iso}"
    } else {
        if ($base.EndsWith("/releases")) {
            return "${base}/${series}/release/${iso}"
        } else {
            return "${base}/releases/${series}/release/${iso}"
        }
    }
}

# Samovar mode detection
$SamovarMode = $env:SAMOVAR_MODE
if ([string]::IsNullOrWhiteSpace($SamovarMode) -and (Test-Path (Join-Path $WorkDir "samovar-config.json"))) {
    $SamovarMode = "samovar"
}
$env:SAMOVAR_MODE = $SamovarMode

$DefaultHostname = if ($SamovarMode -eq "samovar") { "samovar" } else { "friend-server" }
$DefaultUsername = if ($SamovarMode -eq "samovar") { "alex" } else { "server" }

# Hostname prompt
$Hostname = $env:HOSTNAME
if (-not (Test-Configured "HOSTNAME")) {
    $inputHost = Read-Host "Hostname [$DefaultHostname]"
    $Hostname = if ([string]::IsNullOrWhiteSpace($inputHost)) { $DefaultHostname } else { $inputHost }
}
$env:HOSTNAME = $Hostname

# Username prompt
$Username = $env:USERNAME
if (-not (Test-Configured "USERNAME")) {
    $inputUser = Read-Host "Linux username [$DefaultUsername]"
    $Username = if ([string]::IsNullOrWhiteSpace($inputUser)) { $DefaultUsername } else { $inputUser }
}
$env:USERNAME = $Username
if ($Username -notmatch '^[a-z_][a-z0-9_-]*$') {
    Write-Error "Error: invalid username '$Username'. Use lowercase letters, digits, underscore, or hyphen (e.g. server)."
    exit 1
}

# Console password prompt (secure hidden input)
function Read-SecretInput([string]$promptText) {
    Write-Host -NoNewline $promptText
    $secret = ""
    while ($true) {
        $key = [System.Console]::ReadKey($true)
        if ($key.Key -eq [System.ConsoleKey]::Enter) {
            Write-Host ""
            break
        } elseif ($key.Key -eq [System.ConsoleKey]::Backspace) {
            if ($secret.Length -gt 0) {
                $secret = $secret.Substring(0, $secret.Length - 1)
            }
        } elseif ($key.KeyChar -ge 32) {
            $secret += $key.KeyChar
        }
    }
    return $secret
}

$Password = $env:PASSWORD
if (-not (Test-Configured "PASSWORD")) {
    while ($true) {
        $p1 = Read-SecretInput "Console password (sudo is passwordless): "
        $p2 = Read-SecretInput "Repeat password: "
        if ([string]::IsNullOrWhiteSpace($p1)) {
            Write-Host "Password cannot be empty."
            continue
        }
        if ($p1 -ne $p2) {
            Write-Host "Passwords do not match."
            continue
        }
        $Password = $p1
        break
    }
}
$env:PASSWORD = $Password

# SSH public key prompt
$SshPublicKey = $env:SSH_PUBLIC_KEY
if (-not (Test-Configured "SSH_PUBLIC_KEY")) {
    $defaultKey = ""
    $searchPaths = @(
        (Join-Path $HOME ".ssh\id_ed25519.pub"),
        (Join-Path $env:USERPROFILE ".ssh\id_ed25519.pub"),
        (Join-Path $HOME ".ssh\id_ecdsa.pub"),
        (Join-Path $env:USERPROFILE ".ssh\id_ecdsa.pub"),
        (Join-Path $HOME ".ssh\id_rsa.pub"),
        (Join-Path $env:USERPROFILE ".ssh\id_rsa.pub")
    )
    foreach ($p in $searchPaths) {
        if (-not [string]::IsNullOrWhiteSpace($p) -and (Test-Path $p)) {
            $content = [System.IO.File]::ReadAllText($p, $Utf8NoBom).Trim()
            if ($content) {
                $defaultKey = $content
                break
            }
        }
    }

    if ($defaultKey) {
        Write-Host "Found SSH public key:"
        Write-Host $defaultKey
        $useDef = Read-Host "Use it? [Y/n]"
        if ($useDef -match '^[Nn]$') {
            $SshPublicKey = Read-Host "Paste SSH public key"
        } else {
            $SshPublicKey = $defaultKey
        }
    } else {
        $SshPublicKey = Read-Host "Paste SSH public key"
    }
}
$env:SSH_PUBLIC_KEY = $SshPublicKey
if ($SshPublicKey -notmatch '^(ssh-|ecdsa-|sk-)') {
    Write-Error "Error: the SSH public key should start with a valid OpenSSH key type (ssh-ed25519, ecdsa-sha2-nistp256, ssh-rsa, etc.)."
    exit 1
}

# NetBird setup key
$NetbirdKey = $env:NETBIRD_SETUP_KEY
if ($SamovarMode -eq "samovar" -and [string]::IsNullOrWhiteSpace($NetbirdKey)) {
    $NetbirdKey = "samovar-managed-via-config"
}
if (-not (Test-Configured "NETBIRD_SETUP_KEY") -and [string]::IsNullOrWhiteSpace($NetbirdKey)) {
    $NetbirdKey = Read-SecretInput "NetBird ONE-OFF setup key: "
}
$env:NETBIRD_SETUP_KEY = $NetbirdKey
if ([string]::IsNullOrWhiteSpace($NetbirdKey)) {
    Write-Error "Error: NetBird setup key cannot be empty."
    exit 1
}

Write-Host ""
Write-Host "Target architecture: $Arch"
Write-Host "Source ISO:          $IsoName"

$sourceIsoPath = Join-Path $WorkDir $IsoName
if (-not (Test-Path $sourceIsoPath)) {
    $mirrorPref = if ($IsoMirror) { $IsoMirror } else { if ($env:ISO_MIRROR) { $env:ISO_MIRROR } else { "auto" } }
    $resolvedUrl = Resolve-IsoUrl $mirrorPref $Arch $UbuntuSeries $IsoName
    Write-Host ""
    Write-Host "Downloading Ubuntu Server $UbuntuVersion ($Arch)..."
    Write-Host "  $resolvedUrl"
    if ($script:CurlCmd) {
        & $script:CurlCmd.Source -fL --progress-bar -C - "$resolvedUrl" -o "$sourceIsoPath"
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Download failed."
            exit 1
        }
    } else {
        [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12 -bor [System.Net.SecurityProtocolType]::Tls13
        (New-Object System.Net.WebClient).DownloadFile($resolvedUrl, $sourceIsoPath)
    }
} else {
    Write-Host "Using existing $IsoName"
}

# APT settings
$AptRegion = if ($env:APT_REGION) { $env:APT_REGION } else { "auto" }
$AptMirror = if ($env:APT_MIRROR) { $env:APT_MIRROR } else { "" }
$AptSecurityMirror = if ($env:APT_SECURITY_MIRROR) { $env:APT_SECURITY_MIRROR } else { "" }
$AptFallback = if ($env:APT_FALLBACK) { $env:APT_FALLBACK } else { "offline-install" }
$SamovarConfigFile = if ($env:SAMOVAR_CONFIG_FILE) { $env:SAMOVAR_CONFIG_FILE } else { "samovar-config.json" }
$AllowedSigners = if ($env:ALLOWED_SIGNERS) { $env:ALLOWED_SIGNERS } else { "" }
$SshPublicKeys = if ($env:SSH_PUBLIC_KEYS) { $env:SSH_PUBLIC_KEYS } else { $SshPublicKey }
$SudoNoPasswd = if ($env:SUDO_NOPASSWD) { $env:SUDO_NOPASSWD } else { "true" }

Write-Host ""
Write-Host "APT mirrors: region=$AptRegion"
if ($AptMirror) { Write-Host "             custom=$AptMirror" }
if ($AptSecurityMirror) { Write-Host "             security=$AptSecurityMirror" }
Write-Host "             fallback=$AptFallback"
Write-Host ""

# Normalize WorkDir for Docker volume mount (forward slashes)
$DockerWorkDir = $WorkDir.Replace('\', '/')

$step1Path = Join-Path $WorkDir ".autoinstall-step1.tmp.sh"
$step2Path = Join-Path $WorkDir ".autoinstall-step2.tmp.sh"

try {
    Write-Host "Generating password hash and autoinstall.yaml..."

    $step1Script = @'
#!/usr/bin/env bash
set -euo pipefail

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssl python3 python3-yaml python3-jsonschema >/dev/null

export PASSWORD_HASH
PASSWORD_HASH="$(openssl passwd -6 "$PASSWORD")"

output="$(mktemp /work/.autoinstall.yaml.XXXXXX)"
trap 'rm -f "$output"' EXIT
python3 /work/render-autoinstall.py > "$output"
chmod 600 "$output"
mv "$output" /work/autoinstall.yaml
trap - EXIT
'@

    [System.IO.File]::WriteAllText($step1Path, ($step1Script -replace "`r`n", "`n"), $Utf8NoBom)

    & docker run --rm `
      -e "HOSTNAME=$Hostname" `
      -e "USERNAME=$Username" `
      -e "PASSWORD=$Password" `
      -e "SSH_PUBLIC_KEY=$SshPublicKey" `
      -e "NETBIRD_SETUP_KEY=$NetbirdKey" `
      -e "ARCH=$Arch" `
      -e "APT_REGION=$AptRegion" `
      -e "APT_MIRROR=$AptMirror" `
      -e "APT_SECURITY_MIRROR=$AptSecurityMirror" `
      -e "APT_FALLBACK=$AptFallback" `
      -e "SAMOVAR_MODE=$SamovarMode" `
      -e "SAMOVAR_CONFIG_FILE=$SamovarConfigFile" `
      -e "ALLOWED_SIGNERS=$AllowedSigners" `
      -e "SSH_PUBLIC_KEYS=$SshPublicKeys" `
      -e "SUDO_NOPASSWD=$SudoNoPasswd" `
      -v "${DockerWorkDir}:/work" `
      -w /work `
      ubuntu:24.04 bash /work/.autoinstall-step1.tmp.sh

    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to generate autoinstall.yaml."
        exit 1
    }

    Write-Host "Building bootable ISO..."

    $destIsoPath = $OutputIsoPath
    if (Test-Path -LiteralPath $destIsoPath) {
        Remove-Item -LiteralPath $destIsoPath -Force
    }

    $step2Script = @'
#!/usr/bin/env bash
set -euo pipefail

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq xorriso python3 python3-yaml >/dev/null

mkdir -p /tmp/iso-build

xorriso \
  -osirrox on \
  -indev "/work/$ISO_NAME" \
  -extract /boot/grub/grub.cfg /tmp/iso-build/grub.cfg \
  -extract /boot/grub/loopback.cfg /tmp/iso-build/loopback.cfg \
  >/dev/null 2>&1

python3 /work/patch-grub.py \
  /tmp/iso-build/grub.cfg \
  /tmp/iso-build/grub-patched.cfg

python3 /work/patch-grub.py \
  /tmp/iso-build/loopback.cfg \
  /tmp/iso-build/loopback-patched.cfg

python3 /work/validate-autoinstall-iso.py \
  /work/autoinstall.yaml \
  /tmp/iso-build/grub-patched.cfg \
  /tmp/iso-build/loopback-patched.cfg

xorriso \
  -indev "/work/$ISO_NAME" \
  -outdev "$OUTPUT_ISO_PATH" \
  -map /tmp/iso-build/grub-patched.cfg /boot/grub/grub.cfg \
  -map /tmp/iso-build/loopback-patched.cfg /boot/grub/loopback.cfg \
  -map /work/autoinstall.yaml /autoinstall.yaml \
  -boot_image any replay

xorriso \
  -indev "$OUTPUT_ISO_PATH" \
  -find /autoinstall.yaml -exec report_lba -- \
  >/dev/null

xorriso \
  -osirrox on \
  -indev "$OUTPUT_ISO_PATH" \
  -extract /autoinstall.yaml /tmp/iso-build/embedded-autoinstall.yaml \
  -extract /boot/grub/grub.cfg /tmp/iso-build/embedded-grub.cfg \
  -extract /boot/grub/loopback.cfg /tmp/iso-build/embedded-loopback.cfg \
  >/dev/null 2>&1

python3 /work/validate-autoinstall-iso.py \
  /tmp/iso-build/embedded-autoinstall.yaml \
  /tmp/iso-build/embedded-grub.cfg \
  /tmp/iso-build/embedded-loopback.cfg
'@

    [System.IO.File]::WriteAllText($step2Path, ($step2Script -replace "`r`n", "`n"), $Utf8NoBom)

    $dockerStep2Args = @(
      "run", "--rm",
      "-e", "ISO_NAME=$IsoName",
      "-e", "OUTPUT_ISO_PATH=$ContainerOutputIsoPath",
      "-v", "${DockerWorkDir}:/work",
      "-w", "/work"
    )
    if ($OutputIsoIsAbsolute) {
        $DockerOutputDir = $OutputIsoDir.Replace('\', '/')
        $dockerStep2Args += @("-v", "${DockerOutputDir}:/output")
    }
    $dockerStep2Args += @("ubuntu:24.04", "bash", "/work/.autoinstall-step2.tmp.sh")
    & docker @dockerStep2Args

    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to build bootable ISO."
        exit 1
    }

    Write-Host "Writing SHA-256 checksum..."
    $shaPath = "${destIsoPath}.sha256"
    $fileHash = (Get-FileHash -LiteralPath $destIsoPath -Algorithm SHA256).Hash.ToLower()
    $shaEntry = "$fileHash  $OutputIsoName`n"
    [System.IO.File]::WriteAllText($shaPath, $shaEntry, $Utf8NoBom)

    Write-Host ""
    Write-Host "Done:"
    Write-Host "  $destIsoPath"
    if (Test-Path $shaPath) {
        Write-Host "  $shaPath"
    }
    Write-Host ""
    Write-Host "Write it to a USB drive with Balena Etcher, Rufus, or Raspberry Pi Imager."
    if ($SamovarMode -ne "samovar") {
        Write-Host "After the server appears in NetBird, delete or revoke the one-off setup key."
    }
}
finally {
    if (Test-Path $step1Path) { Remove-Item $step1Path -Force -ErrorAction SilentlyContinue }
    if (Test-Path $step2Path) { Remove-Item $step2Path -Force -ErrorAction SilentlyContinue }
}
