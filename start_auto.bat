@echo off
chcp 65001 >nul
echo 🚀 Запуск бота-моста...

:: Запуск Flask в фоне
start "Flask Server" /min cmd /c "venv\Scripts\activate && python flask_app.py"

:: Ждем 3 секунды, пока Flask запустится
echo ⏳ Ожидание запуска Flask...
timeout /t 3 /nobreak >nul

:: Запускаем cloudflared и перехватываем его вывод
echo 🌉 Запуск Cloudflare Tunnel...
for /f "tokens=*" %%a in ('cloudflared.exe tunnel --url http://localhost:5000 2^>^&1') do (
    echo %%a | findstr /i "trycloudflare.com" >nul
    if not errorlevel 1 (
        for /f "tokens=*" %%u in ('echo %%a ^| findstr /o "https://[^ ]*trycloudflare.com"') do (
            set "TUNNEL_URL=%%u"
        )
    )
)