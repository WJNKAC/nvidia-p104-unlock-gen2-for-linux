@echo off
title P104 PCIe Gen2 x4 - Uninstall
cd /d "%~dp0"

echo ============================================================
echo   P104-100 PCIe Gen2 x4 - Uninstall
echo ============================================================
echo.

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Administrator rights required.
    echo         Right-click uninstall.cmd and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)

echo [1/4] Stopping the hold process (graceful)...
schtasks /end /tn "P104 PCIe Gen2x4 Bring-up" >nul 2>&1
rem ------------------------------------------------------------------
rem We do NOT force-kill the process.
rem Force-killing a process that holds a kernel driver handle can leave
rem the driver in a bad state (tool cannot reconnect until reboot).
rem Instead: wait up to 8 seconds; if it still runs, tell the user to
rem reboot FIRST and then run this script again.
rem ------------------------------------------------------------------
set WAITN=0
:WAITLOOP
timeout /t 1 /nobreak >nul 2>&1
tasklist /FI "IMAGENAME eq P104Gen2x4.exe" 2>nul | find /I "P104Gen2x4.exe" >nul
if errorlevel 1 goto PROCGONE
set /a WAITN+=1
if %WAITN% LSS 8 goto WAITLOOP
echo.
echo   [NOTICE] The hold process did not exit on its own.
echo            It is holding a kernel driver handle.
echo            This script will NOT force-kill it (that can break the driver).
echo.
echo            Please REBOOT, then run uninstall.cmd again
echo            to finish removing the autostart task.
echo.
pause
exit /b 2
:PROCGONE
echo       done

echo [2/4] Removing autostart task, drivers and registry policy...
"%~dp0P104Gen2x4.exe" -uninstall

echo [3/4] Restoring GPU registers...
"%~dp0P104Gen2x4.exe" -off

echo [4/4] Deleting installed files...
if exist "C:\Program Files\P104Gen2x4" rmdir /S /Q "C:\Program Files\P104Gen2x4"
if exist "C:\ProgramData\P104Gen2x4" rmdir /S /Q "C:\ProgramData\P104Gen2x4"
echo       done

echo.
echo ============================================================
echo   Uninstalled. Please REBOOT to fully restore the GPU.
echo ============================================================
pause
