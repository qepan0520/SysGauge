@echo off
setlocal
cd /d "%~dp0"

set "RELEASE_TAG=%~1"

echo ============================================
echo  SysGauge build script
echo  Usage: build.bat [V02]
echo ============================================

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Creating .venv ...
    python -m venv .venv
    if errorlevel 1 goto :error
) else (
    echo [1/4] .venv already exists, skipped
)

echo [2/4] Installing / checking dependencies ...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 goto :error

echo [3/4] Building with PyInstaller ...
".venv\Scripts\python.exe" -m PyInstaller --clean --noconfirm sysgauge.spec
if errorlevel 1 goto :error

echo [4/4] Archiving release ...
if not "%RELEASE_TAG%"=="" (
    if not exist "dist\versions" mkdir "dist\versions"
    copy /y "dist\SysGauge.exe" "dist\versions\SysGauge_%RELEASE_TAG%.exe" >nul
    if errorlevel 1 goto :error
    echo Archived: %cd%\dist\versions\SysGauge_%RELEASE_TAG%.exe
) else (
    echo No release tag, only dist\SysGauge.exe was generated
    echo For an official release run: build.bat V02
)

echo.
echo Done: %cd%\dist\SysGauge.exe
goto :eof

:error
echo.
echo *** Build failed, check the messages above ***
exit /b 1
