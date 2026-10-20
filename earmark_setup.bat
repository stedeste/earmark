@echo off
echo.
echo   Earmark Setup
echo   ================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo   X Python not found.
    echo     Install it from https://www.python.org/downloads/
    echo     Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version 2^>^&1') do echo   OK %%i

:: Install dependencies
echo.
echo   Installing dependencies...
pip install -r requirements.txt --quiet

if errorlevel 1 (
    echo   X Failed to install dependencies.
    echo     Try: pip install flask pywebview
    echo.
    pause
    exit /b 1
)

echo   OK Dependencies installed

echo.
echo   Setup complete!
echo.
echo   To start Earmark:
echo      Double-click Earmark.bat
echo      Or run: python earmark.py
echo.
pause
