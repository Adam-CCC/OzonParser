@echo off
chcp 65001 >nul
REM Запускает мониторинг и автоматически перезапускает его, если скрипт упадёт.
REM Оставь это окно открытым, пока хочешь получать уведомления.

:loop
python ozon_monitor.py --config config.json
echo.
echo Скрипт остановился. Перезапуск через 10 секунд... (Ctrl+C чтобы выйти совсем)
timeout /t 10 /nobreak > nul
goto loop
