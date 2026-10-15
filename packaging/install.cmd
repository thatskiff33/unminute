@echo off
rem Install Unminute for this Windows user (no administrator rights needed).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 pause
