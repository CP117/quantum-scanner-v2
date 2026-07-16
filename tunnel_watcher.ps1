# =============================================================
#  tunnel_watcher.ps1 - Cloudflare Quick Tunnel URL watcher
# =============================================================
#
# Purpose:
#   Watches cloudflared's log file for the `https://<random>.trycloudflare.com`
#   URL that Cloudflare prints once the tunnel is fully negotiated.
#   The initial capture loop inside start.bat waits only 12s — cloudflared
#   frequently takes 20-90s to publish the URL when the user's ISP has
#   poor Cloudflare routing.  This background watcher extends the capture
#   window to 3 minutes without blocking the main launcher script.
#
# Invocation (from start.bat):
#   powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass \
#     -File "tunnel_watcher.ps1" "<cfd-log-path>" "<public-url-file>"
#
# Arguments:
#   $args[0]  path to the cloudflared stdout+stderr log
#   $args[1]  path to app\data\public_url.txt
#
# Behavior:
#   - Polls the log every 1s for up to 180s.
#   - Once a trycloudflare URL is found, atomically prepends it to
#     public_url.txt (preserving any LAN + localhost lines already
#     written by the launcher) and exits.
#   - If 180s elapse without a URL, exits silently — the backend's
#     `ensure_public_url_on_startup()` hook will kick in as a last-resort
#     fallback.
#
# Robustness:
#   - Handles missing/locked log files (retries next tick).
#   - Never overwrites existing content; only prepends the URL.
#   - Safe to run multiple copies (the second one will find the URL
#     already published and no-op).

$ErrorActionPreference = 'SilentlyContinue'

$logPath = $args[0]
$urlFile = $args[1]

if (-not $logPath -or -not $urlFile) {
    exit 1
}

$urlPattern = 'https://[a-z0-9-]+\.trycloudflare\.com'
$deadline = (Get-Date).AddSeconds(180)
$captured = $null

while ((Get-Date) -lt $deadline) {
    if (Test-Path -LiteralPath $logPath) {
        try {
            $content = Get-Content -LiteralPath $logPath -Raw -ErrorAction SilentlyContinue
            if ($content) {
                $m = [regex]::Match($content, $urlPattern)
                if ($m.Success) {
                    $captured = $m.Value
                    break
                }
            }
        } catch {
            # File was momentarily locked by cloudflared; retry next tick.
        }
    }

    # Bail early if the URL file already has a trycloudflare URL (e.g. the
    # backend's fallback hook or a concurrent watcher instance published
    # one first — no work needed).
    if (Test-Path -LiteralPath $urlFile) {
        try {
            $existing = Get-Content -LiteralPath $urlFile -Raw -ErrorAction SilentlyContinue
            if ($existing -and [regex]::IsMatch($existing, $urlPattern)) {
                exit 0
            }
        } catch { }
    }

    Start-Sleep -Seconds 1
}

if (-not $captured) {
    exit 0
}

# Atomically prepend the captured URL to the URL file.  Read the existing
# content first, then write [URL, existing lines].  We ALSO strip any
# lines that are obvious pollution (the "ECHO is off." string from a
# previous launcher bug, empty lines, lines that don't start with http).
$existingLines = @()
if (Test-Path -LiteralPath $urlFile) {
    try {
        $existingLines = @(Get-Content -LiteralPath $urlFile -ErrorAction SilentlyContinue |
            Where-Object {
                $_ -and
                $_.Trim() -ne '' -and
                $_ -match '^\s*https?://'
            })
    } catch {
        $existingLines = @()
    }
}

# Remove any prior trycloudflare URL from the existing list — we only
# want ONE public URL in the file at a time.
$existingLines = @($existingLines | Where-Object { $_ -notmatch $urlPattern })

$newContent = @($captured) + $existingLines
try {
    Set-Content -LiteralPath $urlFile -Value $newContent -Encoding UTF8 -ErrorAction Stop
} catch {
    # If the write fails (file locked by backend), retry once after 500ms.
    Start-Sleep -Milliseconds 500
    try {
        Set-Content -LiteralPath $urlFile -Value $newContent -Encoding UTF8
    } catch { }
}

exit 0
