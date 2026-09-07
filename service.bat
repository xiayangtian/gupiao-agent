@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

set "HOST=127.0.0.1"
if defined APP_HOST set "HOST=%APP_HOST%"
set "PORT=8000"
if defined APP_PORT set "PORT=%APP_PORT%"
set "PID_FILE=logs\app.pid"
if defined APP_PID_FILE set "PID_FILE=%APP_PID_FILE%"
set "LOG_FILE=logs\uvicorn.log"
set "ERROR_LOG=logs\uvicorn-error.log"
if defined APP_LOG_FILE set "LOG_FILE=%APP_LOG_FILE%"
if defined APP_ERROR_LOG set "ERROR_LOG=%APP_ERROR_LOG%"
set "HEALTH_URL=http://%HOST%:%PORT%/api/health"
set "PYTHON=.venv\Scripts\python.exe"
if defined APP_PYTHON set "PYTHON=%APP_PYTHON%"

if "%~1"=="" goto menu
if /i "%~1"=="start" goto start_service
if /i "%~1"=="stop" goto stop_service
if /i "%~1"=="restart" goto restart_service
if /i "%~1"=="status" goto status_service
if /i "%~1"=="logs" goto show_logs
if /i "%~1"=="help" goto usage
if /i "%~1"=="-h" goto usage
if /i "%~1"=="--help" goto usage
echo [错误] 未知命令：%~1
goto usage_error

:menu
cls
echo ========================================
echo         股票分析服务 - Windows 控制台
echo ========================================
echo   1. 启动服务
echo   2. 停止服务
echo   3. 重启服务
echo   4. 查看状态
echo   5. 查看最近日志
echo   0. 退出
echo ========================================
set /p "CHOICE=请选择 [0-5]: "
if "%CHOICE%"=="1" goto start_service_menu
if "%CHOICE%"=="2" goto stop_service_menu
if "%CHOICE%"=="3" goto restart_service_menu
if "%CHOICE%"=="4" goto status_service_menu
if "%CHOICE%"=="5" goto show_logs_menu
if "%CHOICE%"=="0" exit /b 0
echo 无效选项。
pause
goto menu

:start_service_menu
call :start_impl
pause
goto menu

:stop_service_menu
call :stop_impl
pause
goto menu

:restart_service_menu
call :stop_impl
if errorlevel 1 (pause & goto menu)
call :start_impl
pause
goto menu

:status_service_menu
call :status_impl
pause
goto menu

:show_logs_menu
call :logs_impl
pause
goto menu

:start_service
call :start_impl
exit /b %errorlevel%

:stop_service
call :stop_impl
exit /b %errorlevel%

:restart_service
call :stop_impl
if errorlevel 1 exit /b %errorlevel%
call :start_impl
exit /b %errorlevel%

:status_service
call :status_impl
exit /b %errorlevel%

:show_logs
call :logs_impl
exit /b %errorlevel%

:start_impl
if not exist "%PYTHON%" (
  echo [错误] 未找到 Python：%PYTHON%
  echo 请先创建虚拟环境并安装依赖，或设置 APP_PYTHON。
  exit /b 1
)
if not exist logs mkdir logs
curl.exe -fsS --max-time 3 "%HEALTH_URL%" >nul 2>&1
if not errorlevel 1 (
  if exist "%PID_FILE%" (set /p "OLD_PID=" < "%PID_FILE%")
  if defined OLD_PID (echo [错误] 服务已在运行 ^(PID !OLD_PID!^)：http://%HOST%:%PORT%) else (echo [错误] 服务已在运行：http://%HOST%:%PORT%)
  exit /b 1
)
if exist "%PID_FILE%" (
  set /p "OLD_PID=" < "%PID_FILE%"
  powershell.exe -NoProfile -Command "if (Get-Process -Id !OLD_PID! -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
  if not errorlevel 1 (
    echo [错误] 服务可能已在运行 ^(PID !OLD_PID!^)，请先执行 stop。
    exit /b 1
  )
  echo 检测到过期 PID 文件，正在清理。
  del /q "%PID_FILE%" >nul 2>&1
)
echo 正在启动服务：http://%HOST%:%PORT%
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$pathValue = $env:PATH; [Environment]::SetEnvironmentVariable('Path', $null, 'Process'); $env:Path = $pathValue; $p = Start-Process -FilePath '%CD%\%PYTHON%' -ArgumentList @('-m','uvicorn','webapp.server:app','--host','%HOST%','--port','%PORT%') -WorkingDirectory '%CD%' -RedirectStandardOutput '%CD%\%LOG_FILE%' -RedirectStandardError '%CD%\%ERROR_LOG%' -WindowStyle Hidden -PassThru; Set-Content -LiteralPath '%CD%\%PID_FILE%' -Value $p.Id -NoNewline"
if errorlevel 1 (
  echo [错误] 无法创建服务进程。
  exit /b 1
)
if not exist "%PID_FILE%" (
  echo [错误] 服务进程未返回 PID。
  exit /b 1
)
set /p "SERVICE_PID=" < "%PID_FILE%"
for /l %%I in (1,1,15) do (
  curl.exe -fsS --max-time 2 "%HEALTH_URL%" >nul 2>&1
  if not errorlevel 1 (
    for /f "tokens=5" %%P in ('netstat.exe -ano ^| findstr.exe /c:":%PORT%" ^| findstr.exe "LISTENING"') do set "SERVICE_PID=%%P"
    if defined SERVICE_PID >"%PID_FILE%" echo !SERVICE_PID!
    echo [成功] 服务已就绪 ^(PID !SERVICE_PID!^)：http://%HOST%:%PORT%
    exit /b 0
  )
  powershell.exe -NoProfile -Command "if (Get-Process -Id !SERVICE_PID! -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
  if errorlevel 1 goto start_failed
  timeout /t 1 /nobreak >nul
)
:start_failed
echo [错误] 服务未能在 15 秒内就绪。
if exist "%ERROR_LOG%" powershell.exe -NoProfile -Command "Get-Content -LiteralPath '%CD%\%ERROR_LOG%' -Tail 30"
call :stop_impl >nul 2>&1
exit /b 1

:stop_impl
if not exist "%PID_FILE%" (
  echo 服务未运行：未找到 %PID_FILE%。
  exit /b 0
)
set /p "SERVICE_PID=" < "%PID_FILE%"
echo(!SERVICE_PID!| findstr /r "^[1-9][0-9]*$" >nul
if errorlevel 1 (
  echo [错误] PID 文件内容无效，已保留该文件：!SERVICE_PID!
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p = Get-CimInstance Win32_Process -Filter 'ProcessId=!SERVICE_PID!' -ErrorAction SilentlyContinue; if (-not $p) { exit 2 }; if ($p.CommandLine -notmatch 'uvicorn' -or $p.CommandLine -notmatch 'webapp\.server:app') { Write-Host ('[错误] PID !SERVICE_PID! 不是本服务：' + $p.CommandLine); exit 3 }"
set "CHECK_RC=!errorlevel!"
if "!CHECK_RC!"=="2" (
  echo 进程 !SERVICE_PID! 已不存在，清理过期 PID 文件。
  del /q "%PID_FILE%" >nul 2>&1
  exit /b 0
)
if not "!CHECK_RC!"=="0" exit /b 1
echo 正在停止服务 ^(PID !SERVICE_PID!^)...
taskkill /pid !SERVICE_PID! /t >nul 2>&1
for /l %%I in (1,1,10) do (
  powershell.exe -NoProfile -Command "if (Get-Process -Id !SERVICE_PID! -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
  if errorlevel 1 goto stopped
  timeout /t 1 /nobreak >nul
)
echo 服务未正常退出，正在强制停止...
taskkill /f /pid !SERVICE_PID! /t >nul 2>&1
if errorlevel 1 (
  echo [错误] 无法终止进程 !SERVICE_PID!，PID 文件已保留。请以管理员身份重试。
  exit /b 1
)
:stopped
del /q "%PID_FILE%" >nul 2>&1
echo [成功] 服务已停止。
exit /b 0

:status_impl
set "SERVICE_PID="
if exist "%PID_FILE%" set /p "SERVICE_PID=" < "%PID_FILE%"
curl.exe -fsS --max-time 3 "%HEALTH_URL%" >nul 2>&1
if not errorlevel 1 (
  if defined SERVICE_PID (echo [运行中] PID !SERVICE_PID!  http://%HOST%:%PORT%) else (echo [运行中] http://%HOST%:%PORT% ^(未找到 PID 文件^))
  powershell.exe -NoProfile -Command "try { $h = Invoke-RestMethod -Uri '%HEALTH_URL%' -TimeoutSec 3; Write-Host ('启动时间：' + $h.started_at); Write-Host ('AI 状态：' + $(if ($h.ai_key_configured) {'已配置'} else {'未配置'})); Write-Host ('索引状态：' + $(if ($h.index_ready) {'就绪'} else {'未就绪'})) } catch {}"
  exit /b 0
)
if defined SERVICE_PID (
  powershell.exe -NoProfile -Command "if (Get-Process -Id !SERVICE_PID! -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
  if not errorlevel 1 (
    echo [异常] 进程 !SERVICE_PID! 存在，但健康检查失败：%HEALTH_URL%
    exit /b 1
  )
)
echo [未运行] 服务没有响应。
exit /b 1

:logs_impl
echo ===== 标准错误：%ERROR_LOG% =====
if exist "%ERROR_LOG%" (powershell.exe -NoProfile -Command "Get-Content -LiteralPath '%CD%\%ERROR_LOG%' -Tail 50") else (echo 暂无日志。)
echo.
echo ===== 标准输出：%LOG_FILE% =====
if exist "%LOG_FILE%" (powershell.exe -NoProfile -Command "Get-Content -LiteralPath '%CD%\%LOG_FILE%' -Tail 50") else (echo 暂无日志。)
exit /b 0

:usage
echo 用法：service.bat [start^|stop^|restart^|status^|logs^|help]
echo 不带参数时显示交互菜单。
echo 可选环境变量：APP_HOST、APP_PORT、APP_PYTHON、APP_PID_FILE、APP_LOG_FILE、APP_ERROR_LOG
exit /b 0

:usage_error
call :usage
exit /b 2
