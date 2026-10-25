@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

:: 1. Check if .venv exists and actually works on THIS PC
:: (If .venv was copied from another PC, pyvenv.cfg points to a missing path and fails)
set "VENV_OK=0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import telegram, paho.mqtt, cryptography, requests, rich, aiohttp" >nul 2>nul
    if !ERRORLEVEL! EQU 0 (
        set "VENV_OK=1"
    ) else (
        ".venv\Scripts\python.exe" -V >nul 2>nul
        if !ERRORLEVEL! NEQ 0 (
            echo [*] Removing .venv copied from another PC...
            rmdir /s /q ".venv"
        )
    )
)

if "!VENV_OK!"=="0" (
    echo ========================================================
    echo   Marstek Bot - Installing Python and Dependencies
    echo ========================================================

    set "PY_EXE="

    for %%P in (
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "C:\Program Files\Python313\python.exe"
        "C:\Program Files\Python312\python.exe"
        "C:\Program Files\Python311\python.exe"
        "C:\Python312\python.exe"
        "C:\ProgramData\miniconda3\python.exe"
        "%USERPROFILE%\miniconda3\python.exe"
    ) do (
        if exist "%%~P" (
            "%%~P" -V >nul 2>nul
            if !ERRORLEVEL! EQU 0 (
                if not defined PY_EXE set "PY_EXE=%%~P"
            )
        )
    )

    if not defined PY_EXE (
        for /f "delims=" %%i in ('where python 2^>nul') do (
            echo %%i | findstr /i "WindowsApps" >nul
            if !ERRORLEVEL! NEQ 0 (
                "%%i" -V >nul 2>nul
                if !ERRORLEVEL! EQU 0 (
                    if not defined PY_EXE set "PY_EXE=%%i"
                )
            )
        )
    )

    if not defined PY_EXE (
        echo [*] Python not found. Downloading Python 3.12.9 x64 installer...
        set "INSTALLER=%TEMP%\python-3.12.9-amd64.exe"
        curl.exe -L -o "!INSTALLER!" "https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe"
        if exist "!INSTALLER!" (
            echo [*] Installing Python 3.12.9...
            "!INSTALLER!" /passive InstallAllUsers=0 PrependPath=1 Include_test=0 Include_pip=1
            del /q "!INSTALLER!" >nul 2>nul
        )
        if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
            set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        )
    )

    if not defined PY_EXE (
        echo [!] Could not find or install Python automatically.
        pause
        exit /b 1
    )

    echo [+] Using Python: !PY_EXE!

    if not exist ".venv\Scripts\python.exe" (
        echo [*] Creating .venv...
        "!PY_EXE!" -m venv .venv
        if !ERRORLEVEL! NEQ 0 (
            "!PY_EXE!" -m pip install --user virtualenv
            "!PY_EXE!" -m virtualenv .venv
        )
    )

    echo [*] Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install "python-telegram-bot[job-queue]>=22.0" "requests>=2.31.0" "rich>=13.0.0" "paho-mqtt>=2.0.0" "aiohttp>=3.9" "cryptography>=42.0.0" "curl_cffi>=0.7.0"
    if !ERRORLEVEL! NEQ 0 (
        echo [!] Dependency install failed.
        pause
        exit /b 1
    )
)

set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

echo [*] Starting Marstek Grid Alert Bot...
".venv\Scripts\python.exe" bot.py
pause
