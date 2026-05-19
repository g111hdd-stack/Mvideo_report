@echo off
chcp 1251 >nul
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python main.py
