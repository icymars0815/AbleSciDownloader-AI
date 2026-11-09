@echo off
rem kytdl <command> [--json]  -  runs python -m ablesci from this folder's .venv
set "APP_ROOT=%~dp0"
set "APP_PY=%APP_ROOT%.venv\Scripts\python.exe"
if not exist "%APP_PY%" set "APP_PY=%APP_ROOT%runtime\python\python.exe"
if not exist "%APP_PY%" set "APP_PY=python"
set "PYTHONPATH=%APP_ROOT%;%PYTHONPATH%"
"%APP_PY%" -m ablesci %*
exit /b %ERRORLEVEL%
