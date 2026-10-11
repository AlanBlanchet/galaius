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
