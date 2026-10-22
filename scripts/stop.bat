@echo off
chcp 65001 >nul
echo 正在停止 CosyVoice 朗读器（占用 7860 端口的进程）...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :7860 ^| findstr LISTENING') do (
    taskkill /f /pid %%a >nul 2>&1
)
echo 已停止。
pause
