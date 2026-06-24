@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ================================
echo   Obsidian Sync Dashboard
echo ================================
echo.
echo Starting server...

start "" http://localhost:8820
python dashboard.py
