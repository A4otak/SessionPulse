@echo off
chcp 65001 > nul
title SessionPulse - Telegram Session + JSON Checker

if not exist ".venv" (
    echo [1/3] Создаю виртуальное окружение...
    python -m venv .venv
)

call .venv\Scripts\activate
echo [2/3] Проверяю зависимости...
pip install -q -r requirements.txt

echo [3/3] Запускаю чекер...
python main.py
pause