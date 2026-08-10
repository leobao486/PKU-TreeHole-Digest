@echo off
setlocal
set "ROOT=%~dp0"
set "CODE=%ROOT%项目代码"

where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher not found. Please install Python 3.11 or newer.
  pause
  exit /b 1
)

if not exist "%CODE%\.venv\Scripts\python.exe" (
  py -3 -m venv "%CODE%\.venv"
  if errorlevel 1 goto :error
)

"%CODE%\.venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :error
pushd "%CODE%"
"%CODE%\.venv\Scripts\python.exe" -m pip install -e ".[dev]"
set "INSTALL_RC=%ERRORLEVEL%"
popd
if not "%INSTALL_RC%"=="0" goto :error

if not exist "%ROOT%个人画像.yaml" copy /Y "%ROOT%个人画像.example.yaml" "%ROOT%个人画像.yaml" >nul
if not exist "%ROOT%每日报告" mkdir "%ROOT%每日报告"

echo Installation completed. You can now run 运行树洞日报.cmd.
pause
exit /b 0

:error
echo Installation failed. Review the messages above.
pause
exit /b 1

