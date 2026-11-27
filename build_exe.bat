@echo off
setlocal
cd /d "%~dp0"
set "PY=py"
where py >nul 2>nul || set "PY=python"

echo Installing build tools...
%PY% -m pip install --upgrade pillow pyinstaller || goto fail

echo Building the .exe (takes a minute)...
%PY% -m PyInstaller --noconfirm --onefile --windowed --name "emotes-downloader" emotes_downloader.py || goto fail

echo.
echo Done! Your app is in the "dist" folder:  dist\emotes-downloader.exe
echo You can copy that .exe anywhere - it doesn't need Python anymore.
pause
exit /b 0

:fail
echo Build failed.
pause
exit /b 1
