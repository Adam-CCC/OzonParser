@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo === Ozon Promo Monitor ===
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo Python не найден в PATH. Установи Python 3.9+ с python.org
    echo и при установке отметь галочку "Add python.exe to PATH".
    pause
    exit /b 1
)

if not exist "config.json" (
    if exist "config.example.json" (
        copy config.example.json config.json >nul
        echo Создал config.json из примера.
        echo Сейчас откроется блокнот — впиши токен Telegram-бота, chat_id
        echo и артикулы товаров ^(см. README.md, раздел 2-3^), сохрани файл,
        echo закрой блокнот и запусти start.bat ещё раз.
        notepad config.json
        pause
        exit /b 0
    ) else (
        echo Не найден ни config.json, ни config.example.json.
        echo Проверь, что запускаешь start.bat из папки со всеми файлами проекта.
        pause
        exit /b 1
    )
)

echo Проверяю/устанавливаю зависимости...
python -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo Не удалось установить зависимости, смотри ошибку выше.
    pause
    exit /b 1
)

echo Проверяю браузер для Playwright ^(Chromium^)...
python -m playwright install chromium
if errorlevel 1 (
    echo Не удалось установить Chromium для Playwright, смотри ошибку выше.
    pause
    exit /b 1
)

echo.
echo Всё готово. Запускаю мониторинг. Оставь это окно открытым.
echo Чтобы остановить совсем — закрой окно или нажми Ctrl+C.
echo Если скрипт упадёт сам по себе — перезапустится через 10 секунд.
echo.

:loop
python ozon_monitor.py --config config.json
echo.
echo Скрипт остановился. Перезапуск через 10 секунд...
timeout /t 10 /nobreak > nul
goto loop
