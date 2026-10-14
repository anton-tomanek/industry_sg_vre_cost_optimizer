@echo off
cd /d "%~dp0\.."
python scripts\start_local_web.py %*
if errorlevel 1 py -3 scripts\start_local_web.py %*
