@echo off
rem Runs one client and appends the output to data\<client>-run.log.
rem Usage: run-client.cmd acme-shop
if "%~1"=="" (
    echo Usage: run-client.cmd CLIENT_NAME
    exit /b 1
)
cd /d "%~dp0"
if not exist data mkdir data
echo ==== %DATE% %TIME% ==== >> "data\%~1-run.log"
".venv\Scripts\pricemon.exe" run "clients\%~1.yaml" >> "data\%~1-run.log" 2>&1
exit /b %ERRORLEVEL%
