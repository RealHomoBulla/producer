@echo off
rem One entry point for the everyday commands (Windows).  producer help
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set "PY=python"
where python >nul 2>nul || set "PY=py -3"
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 (
  echo Python 3.11+ is required but was not found or is too old. Install it from https://www.python.org/downloads/ and run this again. 1>&2
  exit /b 1
)
set "CMD=%~1"
if "%CMD%"=="" set "CMD=help"
shift
if /i "%CMD%"=="setup"  %PY% tools\setup.py %1 %2 %3 %4 %5 %6 %7 %8 %9 & exit /b %errorlevel%
if /i "%CMD%"=="doctor" %PY% tools\producer.py doctor & exit /b %errorlevel%
if /i "%CMD%"=="status" %PY% tools\producer.py status & exit /b %errorlevel%
if /i "%CMD%"=="start"  %PY% tools\guardian.py start & exit /b %errorlevel%
if /i "%CMD%"=="stop"   %PY% tools\guardian.py stop & exit /b %errorlevel%
if /i "%CMD%"=="serve"  %PY% tools\serve.py %1 %2 %3 %4 & exit /b %errorlevel%
if /i "%CMD%"=="update" %PY% tools\update.py %1 %2 & exit /b %errorlevel%
if /i "%CMD%"=="docs"   %PY% tools\doc_check.py & exit /b %errorlevel%
if /i "%CMD%"=="test" (
  %PY% -c "import pytest" >nul 2>nul || (echo pytest is missing: %PY% -m pip install -r requirements-dev.txt 1>&2 & exit /b 1)
  %PY% -m pytest -q tools\tests -p no:cacheprovider
  exit /b %errorlevel%
)
echo usage: producer setup ^| doctor ^| status ^| start ^| stop ^| serve ^| update ^| docs ^| test
echo   update  safe fast-forward from your own origin (refuses over uncommitted work)
echo   setup   first-run wizard          doctor  is this machine ready?
echo   status  one screen of state       start   start the Guardian (opens the Producer tab)
echo   stop    stop the Guardian         serve   optional local dev server for a web product
echo   docs    check the docs            test    run the tool tests
if /i "%CMD%"=="help" exit /b 0
exit /b 2
