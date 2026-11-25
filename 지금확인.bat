@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "PC실행_올리기.ps1"
start "" "data\입찰현황.html"

