# Keep the JSE Task Log web app running in the background.
# Safe to run again and again: starts the server only if it is down.
$ErrorActionPreference = "SilentlyContinue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$port = 8000
$log = Join-Path $here "watchdog.log"

function Write-Log([string]$msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    try {
        if ((Test-Path $log) -and ((Get-Item $log).Length -gt 500KB)) {
            Move-Item $log ($log + ".old") -Force
        }
        Add-Content -Path $log -Value $line -Encoding UTF8
    } catch {}
}

function Get-Python {
    $pin = Join-Path $here "python_for_server.txt"
    if (Test-Path $pin) {
        $p = (Get-Content $pin -Raw).Trim()
        if ($p -and (Test-Path $p) -and ($p -notmatch "WindowsApps")) { return $p }
    }
    try {
        $exe = (& py -c "import sys; print(sys.executable)" 2>$null | Select-Object -Last 1)
        if ($exe -and ($exe -notmatch "WindowsApps")) {
            $w = Join-Path (Split-Path $exe) "pythonw.exe"
            if (Test-Path $w) { return $w }
            if (Test-Path $exe) { return $exe }
        }
    } catch {}
    return $null
}

function Test-PortOpen {
    $c = $null
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $iar = $c.BeginConnect("127.0.0.1", $port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(1500, $false)
        if (-not $ok) { return $false }
        $c.EndConnect($iar)
        return $true
    } catch {
        return $false
    } finally {
        if ($c) { $c.Close() }
    }
}

function Get-ListenerPid {
    try {
        $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop |
            Select-Object -First 1
        if ($c) { return [int]$c.OwningProcess }
    } catch {}
    return $null
}

if (Test-PortOpen) {
    Write-Log "ok - page is answering on port $port"
    exit 0
}

$listenPid = Get-ListenerPid
if ($listenPid) {
    $proc = Get-Process -Id $listenPid -ErrorAction SilentlyContinue
    $name = if ($proc) { $proc.ProcessName } else { "?" }
    if ($name -match "python") {
        Write-Log "port $port is listening (pid $listenPid) but the page is not answering - restarting"
        Stop-Process -Id $listenPid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    } else {
        Write-Log "port $port is in use by '$name' (pid $listenPid) - not killing it"
        exit 1
    }
}

$python = Get-Python
if (-not $python) {
    Write-Log "ERROR: could not find Python. Run install_autostart.ps1 once."
    exit 1
}

# Pass only the filename. The folder name has a space; a full path here
# gets split and Python tries to open "JSE_Internal" instead of the script.
Write-Log "starting $python server.py"
Start-Process -FilePath $python -ArgumentList "server.py" -WorkingDirectory $here -WindowStyle Hidden

$ok = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 400
    if (Test-PortOpen) { $ok = $true; break }
}
if ($ok) {
    Write-Log "started - page is up"
    exit 0
}
Write-Log "ERROR: started Python but the page did not answer on port $port"
exit 1
