@echo off
cd /d "%~dp0"
title Updating Moo
echo Updating Moo's components...
if not exist ".venv\Scripts\python.exe" (
  echo Moo isn't installed in this folder yet. Run install.bat instead.
  pause
  exit /b 1
)
taskkill /f /im pythonw.exe /fi "WINDOWTITLE eq Moo" >nul 2>&1
".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet || (echo Update failed, see above. & pause & exit /b 1)
echo.
echo Done. Start Moo from your desktop.
pause
