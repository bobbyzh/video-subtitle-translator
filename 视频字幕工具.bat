@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "runtime\python\pythonw.exe" (
  echo [错误] 缺少运行环境 runtime\python，请先运行「安装依赖.bat」。
  pause
  exit /b 1
)
start "" "runtime\python\pythonw.exe" "app\main.pyw"
