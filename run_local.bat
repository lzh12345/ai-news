@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === AI 资讯推送 本地 dry-run(只打印,不推送) ===
echo.
python ai_news_push.py --dry-run
echo.
pause
