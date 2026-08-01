@echo off
cd /d "%~dp0"
if "%~1"=="" (
  python crop_to_drawio.py
) else (
  python crop_to_drawio.py %*
)
if errorlevel 1 pause
