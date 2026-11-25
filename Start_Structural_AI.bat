@echo off
title Structural AI Office - Launcher
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch_office.ps1" %*
if errorlevel 1 pause
