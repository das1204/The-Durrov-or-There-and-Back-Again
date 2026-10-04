import os, random, requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

TG_TOKEN = os.getenv('TG_TOKEN')
VK_TOKEN = os.getenv('VK_TOKEN')
VK_GROUP_ID = os.getenv('VK_GROUP_ID')
VK_CONFIRMATION = os.getenv('VK_CONFIRMATION')

CHAT_MAPPING = {}
for pair in os.getenv('CHAT_MAPPING', '').split(','):
    if ':' in pair:
        tg_id, vk_id = pair.split(':')
        CHAT_MAPPING[int(tg_id)] = int(vk_id)
VK_TO_TG = {vk: tg for tg, vk in CHAT_MAPPING.items()}

app = Flask(__name__)

@app.route('/tg_webhook', methods=['POST'])
def tg_webhook():
    try:
        update = request.json
        if 'message' not in update: return jsonify({"ok": True})
        msg = update['message']
        tg_chat_id = msg['chat']['id']
        if tg_chat_id not in CHAT_MAPPING: return jsonify({"ok": True})
        
        vk_user_id = CHAT_MAPPING[tg_chat_id]
        text = msg.get('text') or msg.get('caption') or ""
        attachment = ""

        if 'photo' in msg:
            file_id = msg['photo'][-1]['file_id']
            file_path = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getFile?file_id={file_id}").json()['result']['file_path']
            img_data = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{file_path}").content
            
            upload_url = requests.get("https://api.vk.com/method/photos.getMessagesUploadServer", params={"peer_id": vk_user_id, "access_token": VK_TOKEN, "v": "5.199"}).json()['response']['upload_url']
            upload_resp = requests.post(upload_url, files={'photo': ('img.jpg', img_data, 'image/jpeg')}).json()
            save_resp = requests.post("https://api.vk.com/method/photos.saveMessagesPhoto", data={"server": upload_resp['server'], "photo": upload_resp['photo'], "hash": upload_resp['hash'], "access_token": VK_TOKEN, "v": "5.199"}).json()['response'][0]
            attachment = f"photo{save_resp['owner_id']}_{save_resp['id']}"

        requests.post("https://api.vk.com/method/messages.send", data={"user_id": vk_user_id, "message": text, "attachment": attachment, "random_id": random.randint(-2**31, 2**31-1), "from_group": 1, "access_token": VK_TOKEN, "v": "5.199"})
    except Exception as e: print(f"Ошибка TG->VK: {e}")
    return jsonify({"ok": True})

@app.route('/vk_callback', methods=['POST'])
def vk_callback():
    data = request.json
    if data.get('type') == 'confirmation': return VK_CONFIRMATION
    try:
        if data.get('type') == 'message_new':
            msg = data['object']['message']
            vk_user_id = msg['from_id']
            if vk_user_id not in VK_TO_TG: return jsonify({"ok": True})
            
            tg_chat_id = VK_TO_TG[vk_user_id]
            text = msg.get('text', '')
            photo_url = None
            if 'attachments' in msg:
                for att in msg['attachments']:
                    if att['type'] == 'photo':
                        photo_url = max(att['photo']['sizes'], key=lambda x: x['width'] * x['height'])['url']
                        break

            if photo_url:
                requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto", json={"chat_id": tg_chat_id, "photo": photo_url, "caption": f"🔵 ВК: {text}" if text else None})
            elif text:
                requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": tg_chat_id, "text": f"🔵 ВК: {text}"})
    except Exception as e: print(f"Ошибка VK->TG: {e}")
    return jsonify({"ok": True})

@app.route('/')
def index(): return "✅ Бот-мост работает!"

if __name__ == '__main__':
    print(f"🚀 Запуск. Привязано чатов: {len(CHAT_MAPPING)}")
    app.run(host='0.0.0.0', port=5000, debug=False)