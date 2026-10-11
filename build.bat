@echo off
setlocal
cd /d "%~dp0"
python -m pip install -r requirements-build.txt || goto :err
python -m PyInstaller --noconfirm --clean mooc.spec || goto :err
set "ISCC=%~dp0.tools\InnoSetup\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo Inno Setup compiler missing. Install Inno Setup 6, then retry.
  exit /b 1
)
"%ISCC%" installer\mooc.iss || goto :err
echo Installer created in release\
exit /b 0
:err
echo Build failed. See the error above.
exit /b 1
