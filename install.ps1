# Install the `interact` CLI on Windows, then connect this computer to your Interact account.
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/AlanBlanchet/interact/main/install.ps1 | iex"
#
# Twin of install.sh: installs uv (the Python tool manager) and a Python if missing, then `interact`
# from GitHub's source archives (no git needed). Run in a PowerShell window, it goes straight on to
# `interact login`. Override the source with $env:INTERACT_REPO = <path-or-git-url>, or another
# source archive of this repository (a branch or tag .zip) with $env:INTERACT_ARCHIVE = <url>.
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

function Install-Interact {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $uvBin = Join-Path $HOME '.local\bin'
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host 'Installing uv (Python tool manager)...'
        # Its own process: uv's installer ends with `exit`, which would close this window.
        Invoke-Tool 'installing uv' { powershell -NoProfile -ExecutionPolicy ByPass -Command 'irm https://astral.sh/uv/install.ps1 | iex' }
        $env:Path = "$uvBin;$env:Path"  # uv edits the user PATH for new windows, not this one
    }

    if ($env:INTERACT_REPO) {
        Write-Host "Installing interact from $($env:INTERACT_REPO)..."
        Invoke-Tool 'installing interact' { uv tool install --force $env:INTERACT_REPO }
    } else {
        Install-FromArchives
    }

    # Put uv's tool folder on PATH for future windows, so `interact` is found there.
    Invoke-Tool 'adding interact to PATH' { uv tool update-shell 2>&1 | Out-Null }
    $bin = (Invoke-Tool 'finding interact' { uv tool dir --bin }).Trim()
    $interact = Join-Path $bin 'interact.exe'

    Write-Host ''
    Write-Host 'interact installed.'
    # `irm | iex` keeps this window's keyboard: ask there, and only when a person is at it.
    if (-not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected) {
        Write-Host 'Connecting this computer to your Interact account...'
        & $interact login
        if ($LASTEXITCODE -ne 0) { Write-Host 'Not connected. Run it again any time:  interact login' }
    } else {
        Write-Host 'Next, connect this computer to your Interact account:  interact login'
    }
    Write-Host "(In a new window ``interact`` is on your PATH; here: $interact)"
    Write-Host ''
    Write-Host 'Also: interact install <claude|cursor|codex|vscode|windsurf|zed|claude-desktop>   # register the MCP server'
    Write-Host '      interact status | interact doctor | interact    # bindings, checks, settings UI'
    Write-Host 'On Windows the browser tools work fully; driving native desktop windows is Linux-only today.'
}

# The main branch and the exact interact-core it pins, as source archives: no git on the computer.
function Install-FromArchives {
    $work = Join-Path ([IO.Path]::GetTempPath()) ("interact-install-" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $work | Out-Null
    try {
        Write-Host 'Downloading interact...'
        $archive = if ($env:INTERACT_ARCHIVE) { $env:INTERACT_ARCHIVE } else { 'https://github.com/AlanBlanchet/interact/archive/refs/heads/main.zip' }
        $zip = Join-Path $work 'interact.zip'
        Invoke-WebRequest -UseBasicParsing -Uri $archive -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath (Join-Path $work 'source')
        $source = (Get-ChildItem -Directory (Join-Path $work 'source') | Select-Object -First 1).FullName
        $pin = [regex]::Match((Get-Content -Raw (Join-Path $source 'pyproject.toml')), 'interact-core\.git@([0-9a-f]{40})')
        if (-not $pin.Success) { throw 'cannot read the pinned interact-core version' }
        $overrides = Join-Path $work 'overrides.txt'
        [IO.File]::WriteAllText($overrides, "interact-core @ https://github.com/AlanBlanchet/interact-core/archive/$($pin.Groups[1].Value).tar.gz`n")
        Write-Host 'Installing interact (this takes a minute the first time)...'
        # From its own folder: uv refuses an --overrides path holding a space (a user name often does).
        Push-Location $work
        try { Invoke-Tool 'installing interact' { uv tool install --force --quiet --overrides overrides.txt $source } } finally { Pop-Location }
    } finally {
        Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
    }
}

Install-Interact
