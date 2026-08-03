@echo off
setlocal

rem ============================================================================
rem  Curation Toolkit launcher for Windows.
rem
rem  Double-click this file. The first run downloads a private, self-contained
rem  copy of Python into a "python-embed" folder next to this script (a few
rem  minutes, needs an internet connection) and installs the app's
rem  dependencies into it. Every run after that just starts the app
rem  (a few seconds) and opens it in your browser.
rem
rem  This does not touch any Python you may already have installed, and does
rem  not require Git. To force a clean reinstall, delete the "python-embed"
rem  folder and run this script again.
rem ============================================================================

set "ROOT=%~dp0"
set "PYDIR=%ROOT%python-embed"
set "PYEXE=%PYDIR%\python.exe"
set "PY_VERSION=3.11.9"
set "PY_ZIP_URL=https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-embed-amd64.zip"
set "GET_PIP_URL=https://bootstrap.pypa.io/get-pip.py"

if exist "%PYEXE%" goto :launch

echo ============================================================
echo  First-time setup - this happens once and takes a few minutes.
echo  Please keep this window open and stay connected to the internet.
echo ============================================================
echo.

echo [1/5] Downloading Python...
powershell -NoProfile -Command ^
  "$ProgressPreference = 'SilentlyContinue'; Invoke-WebRequest -Uri '%PY_ZIP_URL%' -OutFile '%TEMP%\python-embed.zip'"
if errorlevel 1 goto :error

echo [2/5] Extracting Python...
mkdir "%PYDIR%" >nul 2>&1
powershell -NoProfile -Command ^
  "Expand-Archive -Path '%TEMP%\python-embed.zip' -DestinationPath '%PYDIR%' -Force"
if errorlevel 1 goto :error
del "%TEMP%\python-embed.zip" >nul 2>&1

echo [3/5] Enabling package installation...
powershell -NoProfile -Command ^
  "Get-ChildItem '%PYDIR%\*._pth' | ForEach-Object { (Get-Content $_.FullName) -replace '#import site','import site' | Set-Content $_.FullName }"
if errorlevel 1 goto :error

echo [4/5] Installing pip...
powershell -NoProfile -Command ^
  "$ProgressPreference = 'SilentlyContinue'; Invoke-WebRequest -Uri '%GET_PIP_URL%' -OutFile '%TEMP%\get-pip.py'"
if errorlevel 1 goto :error
"%PYEXE%" "%TEMP%\get-pip.py" --no-warn-script-location
if errorlevel 1 goto :error
del "%TEMP%\get-pip.py" >nul 2>&1

echo [5/5] Installing app dependencies (this is the slow part)...
"%PYEXE%" -m pip install --no-warn-script-location -r "%ROOT%requirements.txt"
if errorlevel 1 goto :error

"%PYEXE%" "%ROOT%setup.py"

echo.
echo ============================================================
echo  Setup complete!
echo ============================================================
echo.

:launch
echo Starting the Curation Toolkit...
echo (A browser tab should open automatically. To stop the app, close
echo  this window or press Ctrl+C.)
echo.
"%PYEXE%" -m streamlit run "%ROOT%app.py" --server.port 8501
goto :eof

:error
echo.
echo ============================================================
echo  Setup failed - see the error message above.
echo  Common causes: no internet connection, or a firewall blocking
echo  python.org / github.com / bootstrap.pypa.io.
echo  You can safely re-run this script to try again.
echo ============================================================
pause
exit /b 1
