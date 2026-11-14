@echo off
rem Builds dist\Tracklister.exe on Windows. Needs Python 3.10+ from python.org.
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt pyinstaller
)
.venv\Scripts\python build.py %*
