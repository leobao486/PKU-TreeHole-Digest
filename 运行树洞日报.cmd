@echo off
set "ROOT=%~dp0"
set "PYTHONW="
for /d %%D in ("%ROOT%*") do if exist "%%~fD\.venv\Scripts\pythonw.exe" set "PYTHONW=%%~fD\.venv\Scripts\pythonw.exe"
if not defined PYTHONW (
  echo Python environment not found. Please open the project README.
  pause
  exit /b 1
)
start "" "%PYTHONW%" -m pku_treehole_digest.gui
