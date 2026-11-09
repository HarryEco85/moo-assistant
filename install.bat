@echo off
setlocal
cd /d "%~dp0"
title Moo Lite installer
echo.
echo  ===  Installing Moo Lite  ===
echo.

rem ---- 0. Refuse to run from inside the zip (Windows runs it from a temp folder) ----
echo "%~dp0" | findstr /i /c:"\Temp\" /c:".zip" >nul
if %errorlevel%==0 (
  echo.
  echo  It looks like you opened install.bat straight from the zip file.
  echo  Please close this, right-click the zip, choose "Extract All...",
  echo  extract to C:\Moo, then run install.bat from that folder.
  echo.
  pause
  exit /b 1
)

rem ---- 1. Find Python 3.12 or 3.11 (installs 3.12 if missing) ----
set "PY="
py -3.12 -c "import sys" >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3.11 -c "import sys" >nul 2>&1 && set "PY=py -3.11"
if not defined PY (
  echo Python 3.12 isn't installed. Installing it now...
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  py -3.12 -c "import sys" >nul 2>&1 && set "PY=py -3.12"
)
if not defined PY (
  echo.
  echo Couldn't install Python automatically.
  echo Please install Python 3.12 from https://www.python.org/downloads/windows/
  echo then run install.bat again.
  start https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
echo Using %PY%

rem ---- 2. Private Python environment for Moo ----
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv || (echo Couldn't create the environment. & pause & exit /b 1)
)
set "VPY=%~dp0.venv\Scripts\python.exe"
"%VPY%" -m pip install --upgrade pip --quiet
echo Installing Moo's components (a few minutes)...
"%VPY%" -m pip install -r requirements.txt || (echo. & echo Install failed, see the messages above. & pause & exit /b 1)

rem ---- 3. GPU speech recognition if there's an NVIDIA card ----
where nvidia-smi >nul 2>&1
if %errorlevel%==0 (
  echo NVIDIA graphics card found: adding fast GPU speech recognition...
  "%VPY%" -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*" --quiet
)

rem ---- 4. Voice, wake word, API key ----
"%VPY%" setup_moo.py

rem ---- 5. Desktop shortcut (and optional start with Windows) ----
set "TARGET=%~dp0.venv\Scripts\pythonw.exe"
set "SCRIPT=%~dp0run_moo.pyw"
powershell -NoProfile -Command "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$d\Moo.lnk\"); $s.TargetPath='%TARGET%'; $s.Arguments='\"%SCRIPT%\"'; $s.WorkingDirectory='%~dp0'; $s.Description='Moo, your assistant'; $s.Save()"
echo Desktop shortcut created.

choice /c YN /m "Start Moo automatically when Windows starts"
if %errorlevel%==1 (
  powershell -NoProfile -Command "$d=[Environment]::GetFolderPath('Startup'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$d\Moo.lnk\"); $s.TargetPath='%TARGET%'; $s.Arguments='\"%SCRIPT%\"'; $s.WorkingDirectory='%~dp0'; $s.Save()"
  echo Moo will start with Windows.
)

echo.
echo  ===  Done. Double-click Moo on your desktop.  ===
echo.
pause
