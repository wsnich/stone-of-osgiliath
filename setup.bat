@echo off
echo.
echo  ========================================
echo   The Stone of Osgiliath - Setup
echo  ========================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python is not installed or not in PATH.
    echo  Please install Python 3.10+ from https://www.python.org/downloads/
    echo  Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

echo  [1/3] Installing Python dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo  [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo  [2/3] Installing browser for web scraping...
python -m patchright install chromium
if errorlevel 1 (
    echo  [WARNING] Patchright browser install failed. Trying Playwright...
    pip install playwright
    python -m playwright install chromium
)

echo.
echo  [3/3] Setting up configuration...
if not exist config.json (
    copy config.example.json config.json
    echo  Created config.json from template.
    echo.
    echo  ========================================
    echo   IMPORTANT: Edit config.json to add your
    echo   Discord token before running the app.
    echo   See README.md for instructions.
    echo  ========================================
) else (
    echo  config.json already exists - skipping.
)

echo.
echo  Setup complete! Run the app with:
echo    python main.py
echo.
echo  Or double-click start.bat
echo.
pause
