@echo off
rem Same as the desktop shortcut, but keeps a console open so you can see errors.
cd /d "%~dp0"
".venv\Scripts\python.exe" run_moo.pyw
echo.
echo Moo closed. Full log: %APPDATA%\MooLite\moo.log
pause
