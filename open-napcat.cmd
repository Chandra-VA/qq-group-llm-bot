@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -c "import json,webbrowser; from pathlib import Path; from urllib.parse import quote; c=json.loads(Path('.runtime/napcat/config/webui.json').read_text(encoding='utf-8')); webbrowser.open('http://127.0.0.1:6099/webui/?token='+quote(c['token']))"
