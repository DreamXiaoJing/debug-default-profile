@echo off
chcp 936 >nul
setlocal EnableExtensions
title 浏览器补丁 · 一键修复（新版）
cd /d "%~dp0"

rem ===========================================================================
rem  浏览器自动更新后，两个补丁都会失效 —— 双击本文件即可重新打好。
rem
rem  用法：
rem    双击本文件                    提权（UAC）→ 自动修复 chrome / edge 最新版
rem    一键修复.bat --dry-run        只看它打算做什么，不写任何文件、不需要管理员
rem    其它参数原样透传给 python      例如  一键修复.bat --only no-debugger
rem
rem  它做的事：找 Python → 检查浏览器是否还开着 → 跑
rem      python patch_browser.py auto --all
rem  auto --all 会自己找品牌和版本，没收录的新版本会现场定位补丁点，
rem  打完自动把补丁点写回 patch_db.json，已经打过的直接跳过。
rem ===========================================================================

set "DRYRUN="
if /i "%~1"=="--dry-run" set "DRYRUN=1"

if not exist "patch_browser.py" (
    echo [FAIL] 没找到 patch_browser.py —— 请把本文件和它放在同一个目录。
    echo.
    pause
    exit /b 1
)

rem ---- 写 Program Files 下的 dll 需要管理员；dry-run 不需要 ----
fltmc >nul 2>&1
if errorlevel 1 if not defined DRYRUN (
    echo 需要管理员权限，正在弹 UAC 提权……
    if "%~1"=="" (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    ) else (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
    )
    exit /b
)

echo ============================================================
echo   浏览器补丁 · 一键修复
if defined DRYRUN echo   （dry-run 模式：只看不动手）
echo ============================================================
echo.

rem ---- 找 Python：优先项目自带的 .venv，其次 py 启动器 / PATH 里的 python ----
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
    where py >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [FAIL] 没找到 Python。装一个 Python 3.11+，或在项目里建好 .venv 再试。
    echo.
    pause
    exit /b 1
)

rem ---- 浏览器还开着就写不进去 ----
set "RUNNING="
for %%B in (chrome.exe msedge.exe) do (
    tasklist /fi "imagename eq %%B" 2>nul | find /i "%%B" >nul && set "RUNNING=1"
)
if defined RUNNING (
    if defined DRYRUN (
        echo [warn] 检测到浏览器还在运行 —— dry-run 不写入，这次先不管它。
        echo.
    ) else (
        echo [warn] 检测到浏览器还在运行，打补丁前必须先完全退出。
        choice /c YN /n /m "现在自动关闭 Chrome / Edge 吗？[Y=关 / N=我自己关] "
        if errorlevel 2 (
            echo.
            echo 已取消，什么都没改。请手动退出浏览器后重新双击本文件。
            echo.
            pause
            exit /b 1
        )
        taskkill /im chrome.exe /f >nul 2>&1
        taskkill /im msedge.exe /f >nul 2>&1
        echo 已关闭浏览器，等 2 秒让文件句柄释放……
        timeout /t 2 /nobreak >nul
        echo.
    )
)

rem ---- 干活：--all 自己找品牌/版本，没收录的新版本自己定位补丁点 ----
rem      其它参数（例如 --only no-debugger）原样透传给 python
set "EXTRA=%*"
if defined DRYRUN set "EXTRA=--dry-run"
%PY% patch_browser.py auto --all %EXTRA%
set "RC=%ERRORLEVEL%"

echo.
echo ============================================================
if "%RC%"=="0" (
    echo [ ok ] 完成，没有报错。
) else (
    echo [FAIL] 有没处理成功的地方，请看上面的 [FAIL] 说明。
)
echo ============================================================
echo 想看完整状态：运行 patch_browser.py（只读、不需要管理员）。
echo.
pause
exit /b %RC%
