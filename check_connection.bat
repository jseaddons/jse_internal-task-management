@echo off
title Task Log - connection check
color 0F
echo.
echo  ============================================================
echo    JSE Task Log  -  connection check
echo.
echo    This only tests whether this PC can reach the task log
echo    server. It changes nothing on your computer.
echo  ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='SilentlyContinue';" ^
  "$addrFile='\\192.168.0.7\Timesheet\tasklog_address.txt';" ^
  "$target=''; $port=8000; $srvName='';" ^
  "if (Test-Path $addrFile) {" ^
  "  $L=@(Get-Content $addrFile);" ^
  "  if ($L.Count -ge 2) { $target=$L[0].Trim(); $port=[int]$L[1].Trim() };" ^
  "  if ($L.Count -ge 3) { $srvName=$L[2].Trim() };" ^
  "} else {" ^
  "  Write-Host '  Could not read the live address from the Timesheet share.' -ForegroundColor Yellow;" ^
  "  Write-Host '  The host PC is probably off, asleep, or the server is not running.' -ForegroundColor Yellow;" ^
  "}" ^
  "if (-not $target) {" ^
  "  Write-Host '';" ^
  "  Write-Host '  RESULT: No server address to test.' -ForegroundColor Red;" ^
  "  Write-Host '          Ask the host PC user to run install_autostart.ps1 (once)' -ForegroundColor Red;" ^
  "  Write-Host '          or double-click run_server.bat on that PC.';" ^
  "  exit 1;" ^
  "}" ^
  "$url='http://'+$target+':'+$port;" ^
  "Write-Host ('  Testing ' + $url + ' ...');" ^
  "Write-Host '';" ^
  "$r = Test-NetConnection -ComputerName $target -Port $port -WarningAction SilentlyContinue;" ^
  "Write-Host ('  Can open port ' + $port + ' : ' + $r.TcpTestSucceeded);" ^
  "Write-Host '';" ^
  "if ($r.TcpTestSucceeded) {" ^
  "  Write-Host '  RESULT: The network is FINE - the server is reachable.' -ForegroundColor Green;" ^
  "  Write-Host '';" ^
  "  Write-Host '  Open this exact address in your browser:';" ^
  "  Write-Host ('     ' + $url) -ForegroundColor Cyan;" ^
  "  Write-Host '';" ^
  "  Write-Host '  Do NOT type localhost:8000 - localhost means YOUR OWN PC,' -ForegroundColor Yellow;" ^
  "  Write-Host '  not the server, so it will never load.' -ForegroundColor Yellow;" ^
  "  Write-Host '  Easiest way: double-click JSE Task Log.url on the shared drive.';" ^
  "} else {" ^
  "  Write-Host '  RESULT: This PC cannot open the port on the server.' -ForegroundColor Red;" ^
  "  Write-Host '          Either the server window is not running on the host PC,';" ^
  "  Write-Host '          or a firewall is blocking it. Send this result to Ali.';" ^
  "}" ^
  "Write-Host '';" ^
  "Write-Host ('  (server PC name: ' + $srvName + ')') -ForegroundColor DarkGray;"

echo.
echo  ------------------------------------------------------------
echo   Please send a photo or copy of this window to Ali.
echo  ------------------------------------------------------------
echo.
pause
