@echo off
setlocal
set "SCRIPT=C:\Users\Avery\Documents\Playground\eggplant\extract_tits_story.py"
set "SOURCE=C:\Users\Avery\Downloads\Trials-in-Tainted-Space-master\Trials-in-Tainted-Space-master"
set "OUTPUT=C:\Users\Avery\Documents\Playground\eggplant\export"
set "LOG=C:\Users\Avery\Documents\Playground\eggplant\winrun.log"

if exist "%LOG%" del "%LOG%"
echo START %DATE% %TIME%> "%LOG%"

"C:\Python314\python.exe" -u "%SCRIPT%" --source-root "%SOURCE%" --output "%OUTPUT%" --folders includes >> "%LOG%" 2>&1
set "EXITCODE=%ERRORLEVEL%"
echo EXITCODE=%EXITCODE%>> "%LOG%"

type "%LOG%"
exit /b %EXITCODE%
