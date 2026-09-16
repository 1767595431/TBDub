@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul

title TBDub mock API
set "TBDUB_MOCK_SYNTHESIS=1"
set "PYTHONUNBUFFERED=1"

echo.
echo  === TBDub 模拟测试接口 ===
echo  合成不跑模型，接口与正式版相同
echo  关掉本窗口即停止服务
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo [错误] 找不到 python，请先安装并加入 PATH。
  pause
  exit /b 1
)

where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo [错误] 找不到 ffmpeg，形象转码和假成片都需要它。
  pause
  exit /b 1
)

where ffprobe >nul 2>&1
if errorlevel 1 (
  echo [错误] 找不到 ffprobe。
  pause
  exit /b 1
)

if not exist "config.yaml" (
  if exist "config.example.yaml" (
    copy /y "config.example.yaml" "config.yaml" >nul
    echo [提示] 已复制 config.example.yaml 为 config.yaml
  )
)

if not exist "data" mkdir "data"

rem 若上次窗口异常退出留下了 Worker，先清掉再启动
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match '(?i)-m\s+server(\.worker)?(\s|$)' -and $_.Name -match '(?i)python' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

echo 启动中（强制模拟合成）...
echo.
echo  控制台: http://127.0.0.1:8811/
echo  对接 Base URL: http://127.0.0.1:8811
echo  文档: TBDub_模拟测试接口文档.md
echo  对接字段仍看: TBDub_API接口文档.md
echo.

python -m server
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" (
  echo.
  echo [错误] 启动失败，退出码 %ERR%。看上面的报错，或 data\worker.log
  pause
)
endlocal
exit /b %ERR%
