# Stop the background Task Log and remove auto-start.
# Does not delete the database or the firewall rule.
$ErrorActionPreference = "SilentlyContinue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = "JSE Task Log Watchdog"
$startupLnk = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\JSE Task Log.lnk"

Write-Host "Removing scheduled task..."
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
if (Test-Path $startupLnk) { Remove-Item $startupLnk -Force }

try {
    $c = Get-NetTCPConnection -LocalPort 8000 -State Listen | Select-Object -First 1
    if ($c) {
        $proc = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -match "python") {
            Stop-Process -Id $proc.Id -Force
            Write-Host "Stopped the running server (pid $($proc.Id))."
        }
    }
} catch {}

Write-Host "Auto-start removed. The team will not be able to load the page until you start it again."
Read-Host "Press Enter to close"
