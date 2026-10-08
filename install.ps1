# Install the `galaius` CLI on Windows, then connect this computer to your Galaius account.
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/AlanBlanchet/galaius/main/install.ps1 | iex"
#
# Twin of install.sh: installs uv (the Python tool manager) and a Python if missing, then `galaius`
# from GitHub's source archives (no git needed). Run in a PowerShell window, it goes straight on to
# `galaius login`. Override the source with $env:GALAIUS_REPO = <path-or-git-url>, or another
# source archive of this repository (a branch or tag .zip) with $env:GALAIUS_ARCHIVE = <url>.
# Windows PowerShell 5.1 and PowerShell 7 alike; your own user, no administrator needed.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest's progress bar slows downloads tenfold on 5.1

# A native program's own verdict is its exit code: Windows PowerShell 5.1 under 'Stop' would turn any
# line it writes to stderr (uv's "already in PATH") into a terminating error.
function Invoke-Tool([string]$What, [scriptblock]$Command) {
    $ErrorActionPreference = 'Continue'
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE); see the lines above" }
}

# uv's own installer, this exact release, its bytes pinned (it pins each uv binary's sha256 in
# turn); the same pins as the Galaius server's installer.
$UvVersion = '0.11.25'
$UvInstallerSha256 = 'e9d26d1b6c34553831c5334189c1e9e821e53bedc5ad9a37d88992b0355af965'

function Install-Galaius {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $uvBin = Join-Path $HOME '.local\bin'
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host "Installing uv $UvVersion (Python tool manager)..."
        $uvInstaller = Join-Path ([IO.Path]::GetTempPath()) ("uv-installer-" + [Guid]::NewGuid().ToString('N') + '.ps1')
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "https://astral.sh/uv/$UvVersion/install.ps1" -OutFile $uvInstaller
            if ((Get-FileHash -Algorithm SHA256 $uvInstaller).Hash.ToLower() -ne $UvInstallerSha256) { throw 'the uv installer is not the expected file (checksum differs); nothing was installed' }
            # Its own process: uv's installer ends with `exit`, which would close this window.
            Invoke-Tool 'installing uv' { powershell -NoProfile -ExecutionPolicy ByPass -File $uvInstaller }
        } finally {
            Remove-Item -Force $uvInstaller -ErrorAction SilentlyContinue
        }
        $env:Path = "$uvBin;$env:Path"  # uv edits the user PATH for new windows, not this one
    }

    if ($env:GALAIUS_REPO) {
        Write-Host "Installing galaius from $($env:GALAIUS_REPO)..."
        Invoke-Tool 'installing galaius' { uv tool install --force $env:GALAIUS_REPO }
    } else {
        Install-FromArchives
    }

    # Put uv's tool folder on PATH for future windows, so `galaius` is found there.
    Invoke-Tool 'adding galaius to PATH' { uv tool update-shell 2>&1 | Out-Null }
    $bin = (Invoke-Tool 'finding galaius' { uv tool dir --bin }).Trim()
    $galaius = Join-Path $bin 'galaius.exe'

    Write-Host ''
    Write-Host 'galaius installed.'
    # $env:GALAIUS_ADDRESS names the server; a computer connected elsewhere moves there when that
    # server holds it (the server moved).
    $server = if ($env:GALAIUS_ADDRESS) { @('--server', $env:GALAIUS_ADDRESS) } else { @() }
    $migrated = $false
    # Named interact until 2026-10-07: a computer that ran it moves its install once, staying connected.
    $configHome = if ($env:XDG_CONFIG_HOME) { $env:XDG_CONFIG_HOME } else { Join-Path $HOME '.config' }
    if ((Test-Path (Join-Path $HOME '.interact')) -or (Test-Path (Join-Path $configHome 'interact'))) {
        & $galaius migrate
        if ($LASTEXITCODE -eq 0) { $migrated = $true }
        else { Write-Host 'galaius: part of the former install was not moved (above); fix it, then run  galaius migrate' }
    }
    # `irm | iex` keeps this window's keyboard: ask there, and only when a person is at it.
    if ($migrated) {
        Write-Host 'Your interact install is now galaius; this computer stays connected.'
    } elseif (-not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected) {
        Write-Host 'Connecting this computer to your Galaius account...'
        & $galaius login @server
        if ($LASTEXITCODE -ne 0) { Write-Host 'Not connected. Run it again any time:  galaius login' }
    } elseif ($server.Count -gt 0) {
        Write-Host "Connecting this computer to $($env:GALAIUS_ADDRESS)..."
        & $galaius login @server --yes
        if ($LASTEXITCODE -ne 0) { Write-Host 'Not connected. Run it again any time:  galaius login' }
    } else {
        Write-Host 'Next, connect this computer to your Galaius account:  galaius login'
    }
    Write-Host "(In a new window ``galaius`` is on your PATH; here: $galaius)"
    Write-Host ''
    Write-Host 'Also: galaius install <claude|cursor|codex|vscode|windsurf|zed|claude-desktop>   # register the MCP server'
    Write-Host '      galaius status | galaius doctor | galaius    # bindings, checks, settings UI'
    Write-Host 'On Windows the browser tools work fully; driving native desktop windows is Linux-only today.'
}

# The main branch and the exact galaius-core it pins, as source archives: no git on the computer.
function Install-FromArchives {
    $work = Join-Path ([IO.Path]::GetTempPath()) ("galaius-install-" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $work | Out-Null
    try {
        Write-Host 'Downloading galaius...'
        # main's exact commit: the archive's folder then names it, and the install knows which build it
        # is (an automatic upgrade never re-installs it). No answer from GitHub's API: main as is.
        $commit = try { (Invoke-RestMethod -UseBasicParsing -Uri 'https://api.github.com/repos/AlanBlanchet/galaius/commits/main').sha } catch { $null }
        $archive = if ($env:GALAIUS_ARCHIVE) { $env:GALAIUS_ARCHIVE } elseif ($commit -match '^[0-9a-f]{40}$') { "https://github.com/AlanBlanchet/galaius/archive/$commit.zip" } else { 'https://github.com/AlanBlanchet/galaius/archive/refs/heads/main.zip' }
        $zip = Join-Path $work 'galaius.zip'
        Invoke-WebRequest -UseBasicParsing -Uri $archive -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath (Join-Path $work 'source')
        $source = (Get-ChildItem -Directory (Join-Path $work 'source') | Select-Object -First 1).FullName
        $pin = [regex]::Match((Get-Content -Raw (Join-Path $source 'pyproject.toml')), 'galaius-core\.git@([0-9a-f]{40})')
        if (-not $pin.Success) { throw 'cannot read the pinned galaius-core version' }
        $overrides = Join-Path $work 'overrides.txt'
        [IO.File]::WriteAllText($overrides, "galaius-core @ https://github.com/AlanBlanchet/galaius-core/archive/$($pin.Groups[1].Value).tar.gz`n")
        Write-Host 'Installing galaius (this takes a minute the first time)...'
        # From its own folder: uv refuses an --overrides path holding a space (a user name often does).
        Push-Location $work
        try { Invoke-Tool 'installing galaius' { uv tool install --force --overrides overrides.txt $source } } finally { Pop-Location }
    } finally {
        Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
    }
}

# uv and galaius write UTF-8; Windows PowerShell reads programs in the console's older code page,
# which garbles a user folder name with an accent and then finds no galaius there.
$consoleEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
try { Install-Galaius } finally { [Console]::OutputEncoding = $consoleEncoding }
