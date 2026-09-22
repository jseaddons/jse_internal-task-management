# One-time setup. After this, the Task Log starts itself when this PC
# boots / you sign in, and restarts if it crashes. No window to keep open.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$watchdog = Join-Path $here "watchdog.ps1"
$taskName = "JSE Task Log Watchdog"
$firewallName = "JSE Task Log (port 8000)"

function Test-Admin {
    $p = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    Write-Host "Re-launching as administrator (needed for the firewall rule)..." -ForegroundColor Yellow
    $arg = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    try {
        Start-Process powershell -Verb RunAs -ArgumentList $arg -Wait
        exit $LASTEXITCODE
    } catch {
        Write-Host "Could not elevate. Continuing with what we can do without admin." -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "============================================================"
Write-Host "  JSE Task Log - permanent auto-start"
Write-Host "============================================================"
Write-Host ""

# Pin the real Python, never the Microsoft Store stub in WindowsApps.
$python = $null
try {
    $exe = (& py -c "import sys; print(sys.executable)" | Select-Object -Last 1)
    if ($exe -and ($exe -notmatch "WindowsApps") -and (Test-Path $exe)) {
        $python = $exe
    }
} catch {}
if (-not $python) {
    Write-Host "Python was not found. Install Python, then run this again." -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}
Set-Content -Path (Join-Path $here "python_for_server.txt") -Value $python -Encoding ASCII
Write-Host "Python: $python"

# Firewall: allow teammates on Domain, Private, and Public.
$firewallOk = $false
if (Test-Admin) {
    try {
        Get-NetFirewallRule -ErrorAction SilentlyContinue |
            Where-Object { $_.DisplayName -eq "python.exe" -and $_.Action -eq "Block" -and $_.Direction -eq "Inbound" } |
            ForEach-Object {
                Disable-NetFirewallRule -Name $_.Name
                Write-Host "Disabled inbound python.exe block ($($_.Profile))."
            }
        if (Get-NetFirewallRule -DisplayName $firewallName -ErrorAction SilentlyContinue) {
            Set-NetFirewallRule -DisplayName $firewallName -Enabled True `
                -Profile Domain,Private,Public -Action Allow | Out-Null
            Write-Host "Firewall rule already there - left enabled on all profiles."
        } else {
            New-NetFirewallRule -DisplayName $firewallName `
                -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow `
                -Profile Domain,Private,Public `
                -Description "Team task-log web app served by server.py" | Out-Null
            Write-Host "Firewall rule created for TCP 8000 (all network profiles)." -ForegroundColor Green
        }
        $firewallOk = $true
    } catch {
        Write-Host "Firewall rule failed: $($_.Exception.Message)" -ForegroundColor Yellow
    }

    # Stop the NIC from powering down overnight (a common "it died by morning" cause).
    Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
        Where-Object { $_.Status -eq "Up" } |
        ForEach-Object {
            try {
                Disable-NetAdapterPowerManagement -Name $_.Name -ErrorAction Stop
                Write-Host ("Disabled power-saving on network card: " + $_.Name)
            } catch {}
        }
} else {
    Write-Host "Not admin - skipped firewall. Right-click this file, Run as administrator." -ForegroundColor Yellow
}

# Scheduled task: at sign-in, and every 5 minutes as a safety net.
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$watchdog`"" `
    -WorkingDirectory $here
$tLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$tRepeat = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($tLogon, $tRepeat) `
    -Settings $settings -Principal $principal `
    -Description "Keeps the JSE Task Log web page running for the team. Do not disable." | Out-Null
Write-Host "Scheduled task '$taskName' registered (sign-in + every 5 minutes)." -ForegroundColor Green

# Startup-folder shortcut: a second way to start after reboot, no admin needed.
$startup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
if (-not (Test-Path $startup)) { New-Item -ItemType Directory -Path $startup | Out-Null }
$lnkPath = Join-Path $startup "JSE Task Log.lnk"
$wsh = New-Object -ComObject WScript.Shell
$lnk = $wsh.CreateShortcut($lnkPath)
$lnk.TargetPath = "powershell.exe"
$lnk.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$watchdog`""
$lnk.WorkingDirectory = $here
$lnk.WindowStyle = 7
$lnk.Description = "Starts the JSE Task Log so the team can open it in a browser"
$lnk.Save()
Write-Host "Startup shortcut: $lnkPath"

# Start it now so the team does not have to wait.
Write-Host ""
Write-Host "Starting the server now..."
& powershell -NoProfile -ExecutionPolicy Bypass -File $watchdog
$watchExit = $LASTEXITCODE

Write-Host ""
if ($watchExit -eq 0) {
    Write-Host "The page is up. Teammates should double-click:" -ForegroundColor Green
    Write-Host "    \\192.168.0.7\Timesheet\JSE Task Log.url" -ForegroundColor Cyan
    Write-Host "They must never use localhost - that is their own PC."
    Write-Host ""
    Write-Host "You can close every window. The server keeps running in the background."
    Write-Host "This PC must stay powered on (sleep is blocked while the server runs)."
} else {
    Write-Host "Auto-start is installed, but the page did not answer yet." -ForegroundColor Yellow
    Write-Host "Check watchdog.log in this folder."
}
if (-not $firewallOk) {
    Write-Host ""
    Write-Host "Still do this once: right-click install_autostart.ps1 -> Run as administrator." -ForegroundColor Yellow
}
Write-Host ""
Read-Host "Press Enter to close"
