@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo ============================================
echo   下载 7B 离线翻译模型（Hunyuan-MT-7B）
echo   约 4.3GB，支持断点续传，中途关掉重开即可继续
echo ============================================
echo.
"%~dp0runtime\python\python.exe" "%~dp0tools\download_model.py"
echo.
pause
