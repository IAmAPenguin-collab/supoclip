@echo off
rem Double-click to watch what the SupoClip worker is doing, live.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\watch-logs.ps1" %*
