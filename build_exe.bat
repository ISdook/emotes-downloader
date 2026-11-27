@echo off
setlocal
cd /d "%~dp0"
set "PY=py"
where py >nul 2>nul || set "PY=python"

echo Installing build tools...
%PY% -m pip install --upgrade pillow customtkinter pyinstaller || goto fail

echo.
echo [1/3] Building the portable .exe...
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name Emotes-Downloader-Portable --icon assets\icon.ico --add-data "assets;assets" --add-data "CHANGELOG.md;." --collect-data customtkinter --hidden-import PIL._tkinter_finder emotes_downloader.py || goto fail

echo.
echo [2/3] Building the app folder for the installer...
%PY% -m PyInstaller --noconfirm --onedir --windowed --name "Emotes Downloader" --icon assets\icon.ico --add-data "assets;assets" --add-data "CHANGELOG.md;." --collect-data customtkinter --hidden-import PIL._tkinter_finder --distpath dist\app --workpath build\app emotes_downloader.py || goto fail

echo.
echo [3/3] Building the setup .exe...
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo Inno Setup 6 isn't installed, so the setup .exe was skipped.
  echo Get it from https://jrsoftware.org/isdl.php if you want it.
  goto done
)
for /f "delims=" %%v in ('%PY% tools\release_notes.py version') do set "VER=%%v"
"%ISCC%" /DAppVersion=%VER% installer.iss || goto fail

:done
echo.
echo Done! Look in the "dist" folder:
echo   Emotes-Downloader-Portable.exe   (just run it)
echo   Emotes-Downloader-Setup.exe      (installer, if Inno Setup is installed)
pause
exit /b 0

:fail
echo Build failed.
pause
exit /b 1
