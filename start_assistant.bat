@echo off
REM Double-click this file to start Sitruuna on Windows.
REM It restarts itself after /restart (code 3) or a crash; close the window to stop.
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
set "CRASHES=0"
:run
python main.py
if %ERRORLEVEL%==3 (set "CRASHES=0" & echo Restarting... & goto run)
if %ERRORLEVEL%==0 goto end
set /a CRASHES+=1
if %CRASHES% GEQ 5 (echo Crashed 5 times in a row, stopping. & python main.py --notify "Sitruuna crashed 5 times in a row and has stopped. Check data\logs\assistant.log and start it again." & goto end)
echo Sitruuna stopped unexpectedly. Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto run
:end
pause
