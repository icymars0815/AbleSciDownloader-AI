@echo off
rem AbleSciDownloader-AI one-step install (Windows 10/11). Safe to run again: it only adds what is missing.
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [install] Python 3.10+ was not found.
  echo [install] Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), or run:
  echo [install]     winget install -e --id Python.Python.3.13
  echo [install] then run install.cmd again.
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [install] Creating .venv ...
  %PY% -m venv .venv
  if errorlevel 1 exit /b 1
)

echo [install] Installing packages ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements-lock.txt
if errorlevel 1 (
  echo [install] Locked versions failed; trying the version ranges in requirements.txt ...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 exit /b 1
)

echo [install] Checking ...
".venv\Scripts\python.exe" kytdl.py doctor
exit /b %ERRORLEVEL%
