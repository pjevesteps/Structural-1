@echo off
title Structural AI Office - Shutdown
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch_office.ps1" -Stop
timeout /t 2 /nobreak >nul
