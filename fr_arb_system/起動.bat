@echo off
chcp 65001 > nul
title PERP FR ARB Dashboard

echo.
echo  ============================================
echo    PERP FR ARB Dashboard
echo  ============================================
echo.

:: Check Python
python --version > nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python が見つかりません。
    echo https://www.python.org からインストールしてください。
    pause
    exit /b 1
)

:: Install dependencies if needed
echo [1/2] 依存ライブラリをチェック中...
pip install flask flask-cors requests -q --disable-pip-version-check

echo [2/2] サーバー起動中... ブラウザが自動で開きます
echo.
echo  停止するには: Ctrl+C を押してください
echo.

python server.py

pause
