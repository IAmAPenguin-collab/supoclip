<#
.SYNOPSIS
  Live, readable view of what SupoClip's worker is doing.

.DESCRIPTION
  Follows the worker container's logs and prints one short line per event:
  task progress (download, transcription, clip picking, rendering), which AI
  model answered, Cloudflare/this-PC transcription pieces, recoveries,
  warnings and errors. Health checks, idle cron runs and the every-20-second
  "waiting for a slot" retries are hidden.

  Run from anywhere:  powershell -ExecutionPolicy Bypass -File scripts\watch-logs.ps1
  Or double-click watch-logs.cmd in the repo folder.

.PARAMETER All
  Show every line, including the noise this view normally hides.

.PARAMETER Since
  How far back to start, e.g. 10m, 2h (default 15m).
#>
param(
    [switch]$All,
    [string]$Since = "15m",
    # Print what is already logged and exit instead of following.
    [switch]$NoFollow,
    # Read saved log lines from a file instead of the worker (for checking the filters).
    [string]$File
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location (Split-Path -Parent $PSScriptRoot)

# Lines that only say "nothing happened".
$noise = @(
    'recording health',
    '^redis_version=',
    'Loaded \d+ encrypted runtime settings',
    "\{'dispatched': 0, 'pending_checked': 0",
    "\{'copied': 0\}",
    "\{'requeued': \[\], 'failed': \[\]\}",
    'cron:(check_media_storage|replicate_pending_media|copy_pending_uploads|dispatch_scheduled_social_posts|cleanup_media|recover_interrupted_video_tasks|refresh_social_metrics)\(\)',
    'cron:\w+ \S\s*$',
    "admission': 'deferred'",
    'Enqueued job .*process_video_task',
    'Worker processing task',
    'process_video_task\(',
    'Video task \S+ admission=deferred',
    'HTTP Request: (GET|POST) http://(redis|postgres)'
)
$noiseRegex = ($noise -join '|')

$lastShown = @{}      # throttle key -> time last printed
$lastDeferred = @{}   # task -> time we last said it is waiting

function Short([string]$text) {
    # Task ids are long UUIDs; the first 8 characters are enough to tell them apart.
    return [regex]::Replace($text, '([0-9a-f]{8})-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(:[0-9a-f]{32})?', '$1')
}

function Show([string]$time, [string]$text, [string]$color) {
    Write-Host ("{0}  " -f $time) -NoNewline -ForegroundColor DarkGray
    Write-Host $text -ForegroundColor $color
}

function Handle([string]$line) {
    $line = $line.Trim()
    if (-not $line) { return }
    if (-not $line.StartsWith('{')) {
        # arq prints a plain copy of each JSON line; the JSON one is used instead.
        if ($All) { Show (Get-Date -Format 'HH:mm:ss') $line 'DarkGray' }
        return
    }
    try { $entry = $line | ConvertFrom-Json } catch { return }
    $msg = [string]$entry.message
    $level = [string]$entry.level
    $time = try { ([datetime]$entry.timestamp).ToLocalTime().ToString('HH:mm:ss') } catch { Get-Date -Format 'HH:mm:ss' }

    if ($All) { Show $time (Short $msg) 'Gray'; return }

    if ($msg -match 'Deferred video task (\S+)') {
        $task = $Matches[1].Substring(0, 8)
        $now = Get-Date
        if (-not $lastDeferred.ContainsKey($task) -or ($now - $lastDeferred[$task]).TotalMinutes -ge 10) {
            $lastDeferred[$task] = $now
            Show $time "[$task] waiting in the queue for the task ahead to finish" 'DarkYellow'
        }
        return
    }
    if ($level -notin @('WARNING', 'ERROR', 'CRITICAL') -and $msg -match $noiseRegex) { return }

    $text = Short $msg
    # "Task 1dc3a6bd: 34% - Transcribing ..." -> "[1dc3a6bd] 34%  Transcribing ..."
    if ($text -match '^Task (\w{8}): (\d+)% - (.*)$') {
        $task, $pct, $what = $Matches[1], $Matches[2], $Matches[3]
        $lastDeferred.Remove($task)
        # Progress messages repeat every few seconds; show each kind at most every 15 s.
        $kind = $what -replace '[\d.,:]+', '#'
        $key = "$task|" + $kind.Substring(0, [Math]::Min(40, $kind.Length))
        $now = Get-Date
        if ($lastShown.ContainsKey($key) -and ($now - $lastShown[$key]).TotalSeconds -lt 15) { return }
        $lastShown[$key] = $now
        $color = if ($what -match 'Complete|finished') { 'Green' } elseif ($what -match 'fail|error') { 'Red' } else { 'Cyan' }
        Show $time ("[{0}] {1,3}%  {2}" -f $task, $pct, $what) $color
        return
    }

    $color = switch ($level) {
        'ERROR' { 'Red' } 'CRITICAL' { 'Red' } 'WARNING' { 'Yellow' }
        default {
            if ($text -match 'completed successfully|finished') { 'Green' }
            elseif ($text -match 'Cloudflare|faster-whisper|Recovered|Starting worker') { 'White' }
            else { 'Gray' }
        }
    }
    Show $time $text $color
}

Write-Host "SupoClip worker - live view (Ctrl+C to stop; -All shows everything)" -ForegroundColor White
Write-Host "Showing the last $Since, then following new lines..." -ForegroundColor DarkGray
if ($File) {
    Get-Content -Encoding UTF8 $File | ForEach-Object { Handle $_ }
    return
}
if ($NoFollow) {
    docker compose logs worker --no-log-prefix --since $Since 2>$null | ForEach-Object { Handle $_ }
    return
}
while ($true) {
    docker compose logs worker --follow --no-log-prefix --since $Since 2>$null | ForEach-Object { Handle $_ }
    # The stream ends when the worker restarts; pick it up again.
    Show (Get-Date -Format 'HH:mm:ss') "worker stopped or restarted; reconnecting..." 'DarkYellow'
    Start-Sleep -Seconds 3
    $Since = "10s"
}
