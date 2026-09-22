@echo off
REM Rebuild the Power BI CSVs, then click Refresh in Power BI Desktop.
REM Default scope is 24139; pass a code to switch (refresh_powerbi.bat 5.2).
cd /d "%~dp0"
if "%~1"=="" (
    python export_powerbi.py --project 24139
) else (
    python export_powerbi.py --project %1
)
echo.
pause
