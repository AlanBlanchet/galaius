# Install the `galaius` CLI on Windows, then connect this computer to your Galaius account.
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/AlanBlanchet/galaius/main/install.ps1 | iex"
#
# Twin of install.sh: installs uv (the Python tool manager) and a Python if missing, then `galaius`
# from GitHub's source archives (no git needed); it asks nothing. With $env:GALAIUS_ADDRESS it goes on
# to `galaius login --detach` (approval in the browser). Override the source with $env:GALAIUS_REPO = <path-or-git-url>, or another
# source archive of this repository (a branch or tag .zip) with $env:GALAIUS_ARCHIVE = <url>.
# Windows PowerShell 5.1 and PowerShell 7 alike; your own user, no administrator needed.
# Every run writes its own log (%LOCALAPPDATA%\galaius\logs\install-<time>.log): each line shown here and every
# program's output, timestamped. A step that fails is named in one sentence; on a computer already connected
# to Galaius the person is asked whether to send that log (with this computer's own key, to its own server).

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest's progress bar slows downloads tenfold on 5.1

# >>> install report (src/galaius/data/install_report.ps1, verbatim: tests/test_install_scripts.py)
# The install-report part every Galaius install.ps1 shares (owner 2026-10-11: "The install script should have
# error logs in case of errors, and if there is an error that happens we must prompt the user to ask to send").
# One copy: galaius/data/install_report.ps1; a server inserts it into the script it serves, the repository's
# install.ps1 carries it verbatim (tests/test_install_scripts.py). The script names each part of its install with
# `Step`, runs programs through `Invoke-Logged`, downloads with `Fetch`, and runs it all with `Invoke-Install`.
# PowerShell names are case-blind: no parameter may be called $speaksFrench.
$speaksFrench = (Get-UICulture).TwoLetterISOLanguageName -eq 'fr'
# French written as \u escapes: this file stays ASCII (Windows PowerShell may read it as Latin-1). {0}, {1}...: the values after.
function Text([string]$En, [string]$Fr) { $pattern = if ($speaksFrench) { [regex]::Unescape($Fr) } else { $En }; $pattern -f $args }
$configRoot = if ($env:XDG_CONFIG_HOME) { $env:XDG_CONFIG_HOME } else { Join-Path $HOME '.config' }
$logs = Join-Path $env:LOCALAPPDATA 'galaius\logs'
$run = @{ Log = Join-Path $logs ('install-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + "-$PID.log"); Step = (Text 'checking this computer' 'la v\u00e9rification de cet ordinateur') }
try { New-Item -ItemType Directory -Force -Path $logs | Out-Null }
catch { $run.Log = Join-Path ([IO.Path]::GetTempPath()) (Split-Path -Leaf $run.Log) }
$utf8 = [Text.UTF8Encoding]::new($false)
function Log([string]$Line) {
    try { [IO.File]::AppendAllText($run.Log, "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Line`r`n", $utf8) } catch {}
}
function Say([string]$Text) { Write-Host $Text; Log $Text }
# What the person reads if this step fails ("The install stopped while <step>: ..."), and a marker in the log.
function Step([string]$En, [string]$Fr) { $run.Step = Text $En $Fr; Log "-- $En" }

function Fetch([string]$Url, [string]$Path) {
    try { Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Path }
    catch { Log $_.Exception.Message; throw "could not download $Url" }
}

# Every line a program writes, on either stream, is shown and logged; its exit code decides, never its
# stderr (Windows PowerShell would turn that into errors).
function Invoke-Logged([scriptblock]$Command, [switch]$Quiet) {
    $ErrorActionPreference = 'Continue'
    & $Command 2>&1 | ForEach-Object { if ($Quiet) { Log "$_" } else { Say "$_" } }
    $global:LASTEXITCODE
}
# A failed run: its step in one sentence, then, on a computer already connected (machine.json holds its key and
# its server), the question; a yes sends the log there, masked of the home folder (the server removes keys, tokens
# and addresses again). A computer never connected holds no key: nothing can be sent, the log's path is said.
function Offer-Report([string]$Sentence) {
    $machine = try { Get-Content -Raw -LiteralPath (Join-Path $configRoot 'galaius\machine.json') | ConvertFrom-Json } catch { $null }
    # https only (plain http to this computer itself); the token's characters checked before it is a header.
    $address = "$(if ($machine) { $machine.server_url })".TrimEnd('/')
    $server = if ($address -match '^https://[^/\s]+' -or $address -match '^http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$') { $address } else { $null }
    $token = if ($machine -and "$($machine.token)" -match '^iwm_[A-Za-z0-9_-]{32,252}$') { "$($machine.token)" } else { $null }
    $kept = Text 'The whole log is in {0}' 'Le journal complet est dans {0}' $run.Log
    if (-not $server -or -not $token) {
        Say (Text '{0} (this computer is not connected to Galaius yet: nothing was sent).' "{0} (cet ordinateur n'est pas encore connect\u00e9 \u00e0 Galaius : rien n'a \u00e9t\u00e9 envoy\u00e9)." $kept)
        return
    }
    $hostName = ([Uri]$server).Authority
    Write-Host (Text 'Send the error report to Galaius ({0})? [Y/n] ' "Envoyer le rapport d'erreur \u00e0 Galaius ({0}) ? [O/n] " $hostName) -NoNewline
    # A pasted line runs in a console: Read-Host. Input piped in: its next line. None, or no console = no.
    $answer = try { if ([Console]::IsInputRedirected) { [Console]::In.ReadLine() } else { Read-Host } } catch { $null }
    if ($null -ne $answer -and [Console]::IsInputRedirected) { Write-Host $answer }
    Log "asked to send the report: $(if ($null -eq $answer) { '(no answer)' } else { $answer })"
    if ($null -eq $answer -or $answer.Trim() -notmatch '^(|o|oui|y|yes)$') { Say $kept; return }
    $detail = Clean-Text ([regex]::Replace([IO.File]::ReadAllText($run.Log, $utf8), [regex]::Escape($HOME), '~', 'IgnoreCase'))
    if ($detail.Length -gt 60000) { $detail = $detail.Substring($detail.Length - 60000) }
    $message = Clean-Text (($Sentence -split "`r?`n")[0])
    $body = @{ id = [Guid]::NewGuid().ToString(); message = $message.Substring(0, [Math]::Min(600, $message.Length)); detail = $detail } | ConvertTo-Json -Compress
    $why = ''
    foreach ($attempt in 1, 2) {  # the same id: a send that did land is never stored twice
        try {
            $receipt = Invoke-RestMethod -UseBasicParsing -Method Post -Uri "$server/v1/machine/install-report" -TimeoutSec 30 -MaximumRedirection 0 `
                -Headers @{ Authorization = "Bearer $token" } -ContentType 'application/json; charset=utf-8' -Body $utf8.GetBytes($body)
            Say (Text 'Report sent to {0} ({1}). {2}.' 'Rapport envoy\u00e9 \u00e0 {0} ({1}). {2}.' $server $receipt.id $kept)
            return
        }
        catch {
            $status = try { [int]$_.Exception.Response.StatusCode } catch { 0 }
            $why = if ($status) { "HTTP $status" } else { $_.Exception.Message }
            Log "report not sent: $why"
            if ($status -ge 400 -and $status -lt 500) { break }
            Start-Sleep -Seconds 3
        }
    }
    Say (Text 'The report was not sent ({0}). {1}.' "Le rapport n'a pas \u00e9t\u00e9 envoy\u00e9 ({0}). {1}." $why $kept)
}

# Text as the server takes it: no terminal colour codes, no control character but tab and line breaks.
function Clean-Text([string]$Text) { [regex]::Replace($Text, '\x1b\[[0-9;]*[A-Za-z]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '') }

# The install: `Describe` and this computer first in the log, then `Install`. A failure is said in one sentence
# (its step and why), then `Offer-Report`.
function Invoke-Install([string]$Describe, [scriptblock]$Install) {
    Log $Describe
    Log "Windows $([Environment]::OSVersion.Version), PowerShell $($PSVersionTable.PSVersion) $($PSVersionTable.PSEdition), $(if ([Environment]::Is64BitOperatingSystem) { '64-bit' } else { '32-bit' }), language $((Get-UICulture).Name)"
    try { & $Install; Log 'done' }
    catch {
        $sentence = Text 'The install stopped while {0}: {1}' "L'installation s'est arr\u00eat\u00e9e pendant {0} : {1}" $run.Step $_.Exception.Message
        Log "ERROR $sentence"
        if ($_.ScriptStackTrace) { Log $_.ScriptStackTrace }
        Write-Host $sentence -ForegroundColor Red
        Offer-Report $sentence
        $global:LASTEXITCODE = 1  # what a caller checks; `exit` would close the person's window
    }
}
# <<< install report

# A native program's own verdict is its exit code: Windows PowerShell 5.1 under 'Stop' would turn any
# line it writes to stderr (uv's "already in PATH") into a terminating error.
function Invoke-Tool([string]$What, [scriptblock]$Command, [switch]$Quiet) {
    $code = Invoke-Logged $Command -Quiet:$Quiet
    if ($code -ne 0) { Log "exit code $code"; throw "$What failed (exit $code); see the lines above" }
}

# uv's own installer, this exact release, its bytes pinned (it pins each uv binary's sha256 in
# turn); the same pins as the Galaius server's installer.
$UvVersion = '0.11.25'
$UvInstallerSha256 = 'e9d26d1b6c34553831c5334189c1e9e821e53bedc5ad9a37d88992b0355af965'

function Install-Galaius {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $uvBin = Join-Path $HOME '.local\bin'
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Step 'getting uv' "l'installation de uv"
        Say "Installing uv $UvVersion (Python tool manager)..."
        $uvInstaller = Join-Path ([IO.Path]::GetTempPath()) ("uv-installer-" + [Guid]::NewGuid().ToString('N') + '.ps1')
        try {
            Fetch "https://astral.sh/uv/$UvVersion/install.ps1" $uvInstaller
            # .NET itself, never Get-FileHash: Windows PowerShell started from PowerShell 7 cannot load it.
            $stream = [IO.File]::OpenRead($uvInstaller)
            try { $digest = -join ([Security.Cryptography.SHA256]::Create().ComputeHash($stream) | ForEach-Object { $_.ToString('x2') }) } finally { $stream.Dispose() }
            if ($digest -ne $UvInstallerSha256) { throw 'the uv installer is not the expected file (checksum differs); nothing was installed' }
            # Its own process: uv's installer ends with `exit`, which would close this window.
            Invoke-Tool 'installing uv' { powershell -NoProfile -ExecutionPolicy ByPass -File $uvInstaller }
        } finally {
            Remove-Item -Force $uvInstaller -ErrorAction SilentlyContinue
        }
        $env:Path = "$uvBin;$env:Path"  # uv edits the user PATH for new windows, not this one
    }

    if ($env:GALAIUS_REPO) {
        Step 'installing Galaius' "l'installation de Galaius"
        Say "Installing galaius from $($env:GALAIUS_REPO)..."
        Invoke-Tool 'installing galaius' { uv tool install --force $env:GALAIUS_REPO }
    } else {
        Install-FromArchives
    }

    Step 'setting up Galaius' 'la configuration de Galaius'
    # Put uv's tool folder on PATH for future windows, so `galaius` is found there.
    Invoke-Tool 'adding galaius to PATH' -Quiet { uv tool update-shell }
    $bin = "$(uv tool dir --bin)".Trim()
    if ($LASTEXITCODE -ne 0) { throw 'uv did not name its tool folder' }
    Log "tools: $bin"
    $galaius = Join-Path $bin 'galaius.exe'
    if (-not (Test-Path -LiteralPath $galaius)) { throw "galaius was installed but is not in $bin" }

    Write-Host ''
    Write-Host 'galaius installed.'
    # $env:GALAIUS_ADDRESS names the server; a computer connected elsewhere moves there when that
    # server holds it (the server moved).
    $server = if ($env:GALAIUS_ADDRESS) { @('--server', $env:GALAIUS_ADDRESS) } else { @() }
    $migrated = $false
    # Named interact until 2026-10-07: a computer that ran it moves its install once, staying connected.
    if ((Test-Path (Join-Path $HOME '.interact')) -or (Test-Path (Join-Path $configRoot 'interact'))) {
        try { Invoke-Tool 'moving the former install' { & $galaius migrate }; $migrated = $true }
        catch { Say 'galaius: part of the former install was not moved (above); fix it, then run  galaius migrate' }
    }
    # Nothing is asked: the sign-in goes on in the browser and in the background.
    if ($migrated) {
        Say 'Your interact install is now galaius; this computer stays connected.'
    } elseif ($server.Count -gt 0) {
        Step 'connecting this computer' 'la connexion de cet ordinateur'
        try { Invoke-Tool 'connecting this computer' { & $galaius login @server --detach } }
        catch { Say 'Not connected. Run it again any time:  galaius login --detach' }
    } else {
        Write-Host 'Next, connect this computer to your Galaius account:  galaius login --server https://<your server>'
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
        Step 'downloading Galaius' 'le t\u00e9l\u00e9chargement de Galaius'
        Say 'Downloading galaius...'
        # main's exact commit: the archive's folder then names it, and the install knows which build it
        # is (an automatic upgrade never re-installs it). No answer from GitHub's API: main as is.
        $commit = try { (Invoke-RestMethod -UseBasicParsing -Uri 'https://api.github.com/repos/AlanBlanchet/galaius/commits/main').sha } catch { Log "GitHub API: $($_.Exception.Message)"; $null }
        $archive = if ($env:GALAIUS_ARCHIVE) { $env:GALAIUS_ARCHIVE } elseif ($commit -match '^[0-9a-f]{40}$') { "https://github.com/AlanBlanchet/galaius/archive/$commit.zip" } else { 'https://github.com/AlanBlanchet/galaius/archive/refs/heads/main.zip' }
        $zip = Join-Path $work 'galaius.zip'
        Log "archive: $archive"
        Fetch $archive $zip
        Expand-Archive -Path $zip -DestinationPath (Join-Path $work 'source')
        $source = (Get-ChildItem -Directory (Join-Path $work 'source') | Select-Object -First 1).FullName
        $pin = [regex]::Match((Get-Content -Raw (Join-Path $source 'pyproject.toml')), 'galaius-core\.git@([0-9a-f]{40})')
        if (-not $pin.Success) { throw 'cannot read the pinned galaius-core version' }
        $overrides = Join-Path $work 'overrides.txt'
        [IO.File]::WriteAllText($overrides, "galaius-core @ https://github.com/AlanBlanchet/galaius-core/archive/$($pin.Groups[1].Value).tar.gz`n")
        Step 'installing Galaius' "l'installation de Galaius"
        Say 'Installing galaius (this takes a minute the first time)...'
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
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) }
catch { Write-Host 'This window has no console: a user folder with an accent may not be found.' }
try { Invoke-Install "Galaius install (source $(if ($env:GALAIUS_REPO) { $env:GALAIUS_REPO } elseif ($env:GALAIUS_ARCHIVE) { $env:GALAIUS_ARCHIVE } else { 'GitHub main' }), server $(if ($env:GALAIUS_ADDRESS) { $env:GALAIUS_ADDRESS } else { 'not named' }))" { Install-Galaius } }
finally { try { [Console]::OutputEncoding = $consoleEncoding } catch {} }
