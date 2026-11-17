@echo off
cd /d "%~dp0"
if not exist output mkdir output
:loop
python bot.py >> output\bot.log 2>&1
timeout /t 10 /nobreak >nul
goto loop
