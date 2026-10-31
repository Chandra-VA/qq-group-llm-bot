@echo off
setlocal DisableDelayedExpansion
if not defined QQ_EXE set "QQ_EXE=D:\QQ.exe"
if not exist "%QQ_EXE%" (
  echo QQ executable not found. Set QQ_EXE to the full path of QQ.exe.
  pause
  exit /b 1
)
cd /d "%~dp0.runtime\napcat" || exit /b 1
chcp 65001 >nul
set "NAPCAT_PATCH_PACKAGE=%cd%\qqnt.json"
set "NAPCAT_LOAD_PATH=%cd%\loadNapCat.js"
set "NAPCAT_INJECT_PATH=%cd%\NapCatWinBootHook.dll"
set "NAPCAT_LAUNCHER_PATH=%cd%\NapCatWinBootMain.exe"
set "NAPCAT_MAIN_PATH=%cd%\napcat.mjs"
"%NAPCAT_LAUNCHER_PATH%" "%QQ_EXE%" "%NAPCAT_INJECT_PATH%" %*
pause
