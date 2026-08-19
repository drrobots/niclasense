@echo off
rem  Nicla archive viewer -- double-click to open.
rem
rem  Where the logs are is in viewer.conf beside this file, so this never needs editing.
rem
rem  The window that appears is the viewer. Closing it stops the viewer; that is the whole
rem  on/off switch, and it is why this is a console script rather than a silent one.

setlocal

rem pushd rather than cd. cmd cannot hold a UNC path as a working directory -- it warns and
rem silently leaves you in C:\Windows, from where viewer.py and viewer.conf are not there.
rem pushd maps a temporary drive letter to the share and popd releases it again, which is
rem what lets this file live on the share next to the logs.
pushd "%~dp0"
if errorlevel 1 goto noshare

set "PYEXE="

rem A bundled interpreter first, if this was installed rather than copied. Then the launcher,
rem then whatever is on PATH.
if exist "%~dp0..\python\python.exe" set "PYEXE=%~dp0..\python\python.exe"
if not defined PYEXE where py >nul 2>&1 && set "PYEXE=py -3"
if not defined PYEXE where python >nul 2>&1 && set "PYEXE=python"
if not defined PYEXE goto nopython

echo Starting the viewer...
%PYEXE% viewer.py --config viewer.conf --open
if errorlevel 1 goto failed

popd
endlocal
exit /b 0

:noshare
echo.
echo Could not reach %~dp0
echo The share is probably not mounted. Connect it and try again.
echo.
pause
endlocal
exit /b 1

:nopython
echo.
echo Python was not found on this machine.
echo Install Python 3, or run this from an installed copy that bundles one.
echo.
pause
popd
endlocal
exit /b 1

:failed
echo.
echo The viewer stopped with an error. The message above says why.
echo.
pause
popd
endlocal
exit /b 1
