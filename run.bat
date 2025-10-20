@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Truth Trader setup && color 0A

rem --- Run from the script's folder ---
cd /d "%~dp0"

echo === Checking for Python 3.11 (py -3.11) ===
py -3.11 -V >nul 2>&1
if errorlevel 1 (
  echo Python 3.11 not found. Installing via winget...
  winget install --id Python.Python.3.11 -e --source winget
  if errorlevel 1 (
    echo [ERROR] winget failed to install Python 3.11. Try running this file "As Administrator".
    pause
    exit /b 1
  )
  echo Re-checking Python...
  py -3.11 -V || (
    echo [ERROR] Python 3.11 was installed but isn't visible yet.
    echo Close this window, open a NEW command prompt, and run this .bat again.
    pause
    exit /b 1
  )
) else (
  for /f "tokens=1-3" %%a in ('py -3.11 -V') do echo Found %%a %%b %%c
)

echo.
echo === Ensuring Git is available (required for git+pip install) ===
git --version >nul 2>&1
if errorlevel 1 (
  echo Git not found. Installing via winget...
  winget install --id Git.Git -e --source winget
  if errorlevel 1 (
    echo [ERROR] winget failed to install Git. Install Git manually and re-run.
    pause
    exit /b 1
  )
)

echo.
echo === Creating virtual environment (.venv) if missing ===
if not exist ".venv\Scripts\python.exe" (
  py -3.11 -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Could not create venv.
    pause
    exit /b 1
  )
) else (
  echo .venv already exists.
)

echo.
echo === Activating virtual environment ===
call ".\.venv\Scripts\activate"
if errorlevel 1 (
  echo [ERROR] Failed to activate venv.
  pause
  exit /b 1
)

echo.
echo Python in venv: 
python -V || (echo [ERROR] Python not working in venv & exit /b 1)

echo.
echo === Upgrading pip ===
python -m pip install --upgrade pip
if errorlevel 1 (
  echo [ERROR] pip upgrade failed.
  pause
  exit /b 1
)

echo.
echo === Installing Python packages ===
python -m pip install "git+https://github.com/stanfordio/truthbrush.git"
if errorlevel 1 (
  echo [ERROR] Installing truthbrush failed.
  pause
  exit /b 1
)

python -m pip install openai anthropic httpx python-dotenv tenacity
if errorlevel 1 (
  echo [ERROR] Installing dependencies failed.
  pause
  exit /b 1
)

echo.
echo === Running main.py ===
python main.py
set EXITCODE=%ERRORLEVEL%

echo.
if %EXITCODE% NEQ 0 (
  echo [ERROR] main.py exited with code %EXITCODE%.
) else (
  echo Done.
)
pause
exit /b %EXITCODE%
