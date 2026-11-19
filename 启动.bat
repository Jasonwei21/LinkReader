@echo off
rem ===================================================================
rem  LinkReader launcher
rem  KEEP THIS FILE 100%% ASCII, CRLF LINE ENDINGS.
rem  cmd.exe parses .bat with the OEM codepage (936 on zh-CN Windows):
rem  non-ASCII bytes get mis-decoded and EAT the neighbouring characters,
rem  which silently breaks the commands below. All Chinese text lives in
rem  launch.py / app.py, never here.
rem ===================================================================
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title LinkReader
setlocal EnableExtensions

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY="
set "PF86=%ProgramFiles(x86)%"

rem --- 0) explicit override: set LINKREADER_PY=C:\path\to\python.exe -----
if defined LINKREADER_PY set "PY=%LINKREADER_PY%"

rem --- 1) launcher / PATH -----------------------------------------------
rem NOTE: "where python" is NOT enough. On machines without a real Python,
rem PATH resolves to %LOCALAPPDATA%\Microsoft\WindowsApps\python.exe, which
rem is a Store redirector stub. It exists, it is found, and it only prints
rem "Python was not found". The only reliable test is to actually run -c.
if not defined PY call :probe py -3
if not defined PY call :probe python
if not defined PY call :probe python3

rem --- 2) usual install folders -----------------------------------------
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if not defined PY call :probe "%%~fD\python.exe"
if not defined PY for /d %%D in ("%ProgramFiles%\Python3*") do if not defined PY call :probe "%%~fD\python.exe"
if not defined PY for /d %%D in ("%PF86%\Python3*") do if not defined PY call :probe "%%~fD\python.exe"
if not defined PY for /d %%D in ("C:\Python3*") do if not defined PY call :probe "%%~fD\python.exe"

rem --- 3) last resort: interpreter shipped with the WorkBuddy runtime ----
if not defined PY for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\3*") do if not defined PY call :probe_bundled "%%~fD\python.exe"

if defined PY goto run
goto nopython


:probe
rem %* = candidate command line. Exit 9009 (or any error) = not usable.
%* -c "import sys" >nul 2>&1
if errorlevel 1 exit /b 0
set "PY=%*"
exit /b 0

:probe_bundled
%* -c "import sys" >nul 2>&1
if errorlevel 1 exit /b 0
set "PY=%*"
echo.
echo  [i] No system Python was found.
echo      Falling back to the Python bundled with the WorkBuddy runtime:
echo      %*
echo.
echo      For a standalone setup, install Python 3.9+ and run this again:
echo          winget install Python.Python.3.12
echo.
exit /b 0


:run
%PY% launch.py %*
if not errorlevel 1 exit /b 0
echo.
echo  [ERROR] LinkReader stopped with an error. Read the message above.
echo.
pause
exit /b 1


:nopython
echo.
echo  [ERROR] No usable Python found on this computer.
echo.
echo  The "python" on your PATH is only the Microsoft Store placeholder;
echo  it cannot run scripts. Install a real Python 3.9 or newer:
echo.
echo    1) winget install Python.Python.3.12
echo    2) or https://www.python.org/downloads/
echo       (tick "Add python.exe to PATH" during setup)
echo.
echo  Then run this file again.
echo.
pause
exit /b 1
