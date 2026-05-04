@echo off
cd /d "%~dp0"
echo NQ Oracle LIVE - Starting local dashboard...
echo.
echo   Live Dashboard:  http://localhost:8888/nq-oracle-live.html
echo   Backtest Tool:   http://localhost:8888/nq-oracle-master-v1.html
echo.
start http://localhost:8888/nq-oracle-live.html
python -m http.server 8888
pause
