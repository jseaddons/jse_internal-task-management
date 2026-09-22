# Allow teammates to reach the task-log app.
#
# Right-click this file -> "Run with PowerShell" as an administrator, or from an
# elevated PowerShell:  powershell -ExecutionPolicy Bypass -File .\allow_firewall_port8000.ps1
#
# Why this keeps breaking: Windows shows "blocked some features of python.exe".
# If anyone clicks Cancel, Windows writes an inbound BLOCK for that python.exe.
# Block beats Allow, so the team cannot load the page while localhost still works.
# A new Python install creates a new prompt, so the problem returns a day later.
# Fix: turn those python.exe blocks off, and allow TCP 8000 by port (not by .exe).

$ErrorActionPreference = "Stop"
$name = "JSE Task Log (port 8000)"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "This needs to run as administrator." -ForegroundColor Red
    Write-Host "Right-click the file and choose 'Run as administrator', then try again."
    Read-Host "Press Enter to close"
    exit 1
}

# These are the "Windows Firewall has blocked python.exe" leftovers.
Get-NetFirewallRule -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -eq "python.exe" -and $_.Action -eq "Block" -and $_.Direction -eq "Inbound" } |
    ForEach-Object {
        Disable-NetFirewallRule -Name $_.Name
        Write-Host "Disabled inbound block: $($_.Name)" -ForegroundColor Green
    }

if (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue) {
    Set-NetFirewallRule -DisplayName $name -Enabled True `
        -Profile Domain,Private,Public -Action Allow | Out-Null
    Write-Host "Rule '$name' already exists - left enabled on all profiles."
} else {
    New-NetFirewallRule -DisplayName $name `
        -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow `
        -Profile Domain, Private, Public `
        -Description "Team task-log web app served by server.py" | Out-Null
    Write-Host "Created firewall rule '$name'." -ForegroundColor Green
}

Get-NetFirewallRule -DisplayName $name |
    Select-Object DisplayName, Enabled, Direction, Action, Profile | Format-Table

Write-Host ""
$ip = (Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway }).IPv4Address.IPAddress | Select-Object -First 1
Write-Host "Now ask a teammate to open:  http://$env:COMPUTERNAME`:8000" -ForegroundColor Cyan
Write-Host "                    or:      http://${ip}:8000" -ForegroundColor Cyan
Write-Host "(Teammates must NOT use localhost - that means their own PC.)" -ForegroundColor Yellow
Write-Host "(or http://$env:COMPUTERNAME:8000 )"
Read-Host "Press Enter to close"
