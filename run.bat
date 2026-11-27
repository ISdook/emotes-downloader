@echo off
setlocal
cd /d "%~dp0"
title Emotes Downloader

set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY goto nopython
%PY% -c "import sys" >nul 2>nul || goto nopython

%PY% -c "import PIL, customtkinter" >nul 2>nul
if errorlevel 1 (
  echo First run: installing what the app needs ^(Pillow + CustomTkinter^)...
  %PY% -m pip install --upgrade pillow customtkinter
  if errorlevel 1 (
    echo.
    echo Couldn't install them. Check your internet connection and try again.
    pause
    exit /b 1
  )
)

%PY% "emotes_downloader.py" %*
if errorlevel 1 pause
exit /b 0

:nopython
echo Python isn't installed.
echo Download it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup, then run this again.
pause
exit /b 1
