@echo off
chcp 65001 >nul
set SCRIPT=%~dp0install_ocr_en.ps1
echo 即将请求管理员权限安装英文 OCR 语言包（Language.OCR en-US）。
echo 弹出的 UAC 窗口请点「是」。
pause
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','%SCRIPT%'"
echo.
echo 安装完成后可关闭本窗口，日志在 %TEMP%\lr_ocr_install.log
pause
