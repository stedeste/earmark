@echo off
rem Installs this plugin once. Same as: py -3 install.py
cd /d "%~dp0"
py -3 install.py
if errorlevel 9009 python install.py
pause
