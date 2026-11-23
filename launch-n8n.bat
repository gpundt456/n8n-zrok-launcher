
@echo off
setlocal

REM n8n + zrok launcher
REM Keep this file in the same folder as start_n8n.py.
REM Configuration and credentials are loaded by Python from .env.

cd /d "%~dp0"

echo ==========================================
echo       n8n + zrok Webhook Launcher
echo ==========================================
echo.

if not exist "start_n8n.py" (
    echo ERROR: start_n8n.py was not found.
    echo Keep both files in the same folder.
    goto :end
)

REM Use the Windows Python launcher if available.
where py >nul 2>&1
if %ERRORLEVEL%==0 (
    py start_n8n.py
    goto :end
)

REM Otherwise use python from PATH.
where python >nul 2>&1
if %ERRORLEVEL%==0 (
    python start_n8n.py
    goto :end
)

echo ERROR: Python was not found.
echo Install Python and add it to your PATH.

:end
echo.
pause
endlocal
