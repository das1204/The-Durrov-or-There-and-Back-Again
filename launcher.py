import subprocess
import time
import re
import requests
import os
from dotenv import load_dotenv

load_dotenv()
TG_TOKEN = os.getenv('TG_TOKEN')
VK_TOKEN = os.getenv('VK_TOKEN')
VK_GROUP_ID = os.getenv('VK_GROUP_ID')

def get_tunnel_url():
    """Запускает cloudflared и парсит URL из его вывода."""
    print("🌉 Запуск Cloudflare Tunnel...")
    process = subprocess.Popen(
        ["./cloudflared-windows-amd64.exe", "tunnel", "--url", "http://localhost:5000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding='utf-8',
        errors='replace'
    )
    
    tunnel_url = None
    for line in process.stdout:
        print(line.strip())
        match = re.search(r'(https://[a-zA-Z0-9\-]+\.trycloudflare\.com)', line)
        if match:
            tunnel_url = match.group(1)
            break
    
    return tunnel_url, process

def update_telegram_webhook(url):
    """Обновляет webhook Telegram."""
    webhook_url = f"{url}/tg_webhook"
    resp = requests.get(
        f"https://api.telegram.org/bot{TG_TOKEN}/setWebhook",
        params={"url": webhook_url}
    ).json()
    if resp.get('ok'):
        print(f"✅ Telegram webhook обновлен: {webhook_url}")
    else:
        print(f"❌ Ошибка Telegram: {resp}")

def update_vk_callback(url):
    """Обновляет Callback API ВК."""
    callback_url = f"{url}/vk_callback"
    resp = requests.post(
        "https://api.vk.com/method/groups.setCallbackServer",
        data={
            "group_id": VK_GROUP_ID,
            "server_url": callback_url,
            "access_token": VK_TOKEN,
            "v": "5.199"
        }
    ).json()
    if 'response' in resp:
        print(f"✅ VK Callback обновлен: {callback_url}")
    else:
        print(f"❌ Ошибка VK: {resp}")
        print("⚠️ Возможно, нужно один раз вручную подтвердить сервер в настройках ВК")

if __name__ == '__main__':
    # 1. Запускаем Flask в фоне
    print("🚀 Запуск Flask-сервера...")
    flask_process = subprocess.Popen(
        ["venv\\Scripts\\python.exe", "flask_app.py"],
        creationflags=subprocess.CREATE_NEW_CONSOLE
    )
    time.sleep(3)
    
    # 2. Запускаем cloudflared и получаем URL
    tunnel_url, tunnel_process = get_tunnel_url()
    
    if not tunnel_url:
        print("❌ Не удалось получить URL туннеля")
        input("Нажмите Enter для выхода...")
        exit(1)
    
    print(f"\n🎉 Туннель создан: {tunnel_url}\n")
    
    # 3. Обновляем webhook'и
    update_telegram_webhook(tunnel_url)
    update_vk_callback(tunnel_url)
    
    print("\n✅ Всё готово! Бот работает.")
    print("⚠️ Не закрывайте это окно и окно Flask.")
    print("Чтобы остановить — нажмите Ctrl+C здесь.\n")
    
    # 4. Держим процесс живым
    try:
        tunnel_process.wait()
    except KeyboardInterrupt:
        print("\n🛑 Остановка...")
        flask_process.terminate()
        tunnel_process.terminate()