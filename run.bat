@echo off
cd /d "%~dp0"
if "%~1"=="" (
  if exist "docs\yolo_architecture_cvpr_demo.png" (
    python crop_to_drawio.py "docs\yolo_architecture_cvpr_demo.png"
  ) else (
    python crop_to_drawio.py
  )
) else (
  python crop_to_drawio.py %*
)
if errorlevel 1 pause
