@echo off
REM ============================================================================
REM TESTRADE Zombie Process Cleanup
REM Kills all TESTRADE processes and frees all ports
REM ============================================================================

cd /d C:\TESTRADE

echo ============================================================================
echo                     TESTRADE ZOMBIE CLEANUP
echo ============================================================================
echo.
echo This will terminate ALL TESTRADE processes and free all ports.
echo.

choice /C YN /M "Continue with cleanup"
if errorlevel 2 exit /b 0

echo.
echo Activating virtual environment...
call .venv\Scripts\activate.bat

echo.
echo Running zombie killer...
python mission_control\server\zombie_killer.py

echo.
echo ============================================================================
echo Cleanup complete. You can now start Mission Control fresh.
echo ============================================================================
echo.

pause