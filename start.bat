@echo off
rem Dwuklik uruchamia skaner na tym komputerze i otwiera panel w przegladarce.
cd /d "%~dp0"
start "" /b cmd /c "ping -n 4 127.0.0.1 >nul & start http://localhost:8080"
where py >nul 2>nul
if %errorlevel%==0 (py skaner.py) else (python skaner.py)
pause
