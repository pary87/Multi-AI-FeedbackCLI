@echo off
rem Double-click to open the Council app. Keep the black window open while you use it.
cd /d "%~dp0"
python -m council_app %*
if errorlevel 1 pause
