@echo off
rem Opens Council with a console that shows its log (for troubleshooting). Day to day, use the Council shortcut.
cd /d "%~dp0"
python -m council_app %*
if errorlevel 1 pause
