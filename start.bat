@echo off
rem ============================================================
rem  web-agent 本地启动器（Windows 双击运行）
rem
rem  做三件事：自动选解释器 → 交给 run.py 完成迁移与启动 →
rem  启动成功后自动打开浏览器。Ctrl+C 或关闭窗口即停止服务。
rem
rem  想跳过自动开浏览器：删掉下面 "--open" 这一段即可，
rem  或改成  "%PY%" run.py %*   （自己敲命令时可用 --no-open 覆盖）
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY="
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

echo [web-agent] 使用解释器：%PY%
echo [web-agent] 启动中，首次运行会先建表并灌入演示数据，请稍候...
echo.

"%PY%" run.py --open %*

set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
    echo [web-agent] 服务已停止。
) else (
    echo [web-agent] 异常退出，退出码 %RC%，请看上面的报错。
)
pause
