@echo off
setlocal
REM Keep tester.yaml, studies\ and reports\ next to the jar, whatever folder this was started from.
cd /d "%~dp0"

echo AIFI Gateway Tester
echo.
echo   [1] Show the test studies in studies\
echo   [2] Test the connection to every gateway port (C-ECHO)
echo   [3] Burst: send a number of studies now and wait for the AI results
echo   [4] Run the schedules from tester.yaml (Ctrl+C to stop)
echo   [5] Open the most recent report
echo   [6] Exit
echo.
set /p CHOICE="Enter choice (1-6): "

if "%CHOICE%"=="1" goto SCAN
if "%CHOICE%"=="2" goto ECHO
if "%CHOICE%"=="3" goto BURST
if "%CHOICE%"=="4" goto RUN
if "%CHOICE%"=="5" goto REPORT
goto END

:SCAN
java -Dfile.encoding=UTF-8 -jar "%~dp0aifi-gateway-tester.jar" scan
pause
goto END

:ECHO
java -Dfile.encoding=UTF-8 -jar "%~dp0aifi-gateway-tester.jar" echo
pause
goto END

:BURST
set /p COUNT="Number of studies: "
set /p TARGET="Only this port name (Enter = random over all): "
if "%TARGET%"=="" (
  java -Dfile.encoding=UTF-8 -jar "%~dp0aifi-gateway-tester.jar" burst %COUNT%
) else (
  java -Dfile.encoding=UTF-8 -jar "%~dp0aifi-gateway-tester.jar" burst %COUNT% --target %TARGET%
)
pause
goto END

:RUN
java -Dfile.encoding=UTF-8 -jar "%~dp0aifi-gateway-tester.jar" run
goto END

:REPORT
for /f "delims=" %%D in ('dir /b /ad /o-n "%~dp0reports" 2^>nul') do (
  start "" "%~dp0reports\%%D\report.html"
  goto END
)
echo No report yet.
pause
goto END

:END
endlocal
