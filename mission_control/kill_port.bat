@echo off
REM ============================================================================
REM Kill Process Using Port
REM Usage: kill_port.bat [port_number]
REM ============================================================================

if "%1"=="" (
    echo Usage: kill_port.bat [port_number]
    echo Example: kill_port.bat 5555
    exit /b 1
)

set PORT=%1

echo Finding process using port %PORT%...
echo.

REM Find the process using the port
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :%PORT%') do (
    set PID=%%a
    goto :found
)

echo No process found using port %PORT%
exit /b 0

:found
echo Found process PID: %PID%

REM Get process name
for /f "tokens=1" %%a in ('tasklist /FI "PID eq %PID%" /NH') do (
    echo Process name: %%a
)

echo.
echo Killing process %PID%...
taskkill /F /PID %PID%

if %errorlevel% == 0 (
    echo Process killed successfully
) else (
    echo Failed to kill process
)