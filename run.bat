@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0scripts\start_web.py" %*
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo 请先安装 Python 3.10 至 3.14，并勾选 Add Python to PATH。
        if "%~1"=="--check" exit /b 1
        if "%~1"=="--dry-run" exit /b 1
        pause
        exit /b 1
    )
    python "%~dp0scripts\start_web.py" %*
)
if "%~1"=="--check" exit /b %errorlevel%
if "%~1"=="--dry-run" exit /b %errorlevel%
if errorlevel 1 (
    echo.
    echo 网站未启动，请按上方提示处理后重试。
    pause
    exit /b 1
)
