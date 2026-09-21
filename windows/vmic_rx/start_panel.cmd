@echo off
cd /d %~dp0
start "" pythonw.exe vmic_panel.py %*
