@echo off
rem Double-click to start the card tracker on Windows.
cd /d "%~dp0"
where py >nul 2>nul && (py server.py) || (python server.py)
pause
