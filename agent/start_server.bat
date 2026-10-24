@echo off
setlocal
cd /d "%~dp0.."
if defined EQUIS_PYTHON (set "APP_PY=%EQUIS_PYTHON%") else (set "APP_PY=%CD%\.venv\Scripts\python.exe")
if not exist "%APP_PY%" (
  echo Install requirements into .venv or set EQUIS_PYTHON.
  exit /b 1
)
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
"%APP_PY%" -B agent\server.py
endlocal
