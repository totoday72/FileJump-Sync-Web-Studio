@echo off
cd /d "%~dp0"
python filejump_sync_web_studio.py
if errorlevel 1 pause