import logging
import os
import random
import time
import requests
from flask import Flask, jsonify, request
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

TG_TOKEN = os.getenv('TG_TOKEN', '')
VK_TOKEN = os.getenv('VK_TOKEN', '')
VK_GROUP_ID = os.getenv('VK_GROUP_ID', '')
VK_CONFIRMATION = os.getenv('VK_CONFIRMATION', 'ok')


def parse_chat_mapping(raw_value: str):
    mapping = {}
    for pair in raw_value.split(','):
        item = pair.strip()
        if not item or ':' not in item:
            continue
        tg_value, vk_value = item.split(':', 1)
        try:
            tg_id = int(tg_value.strip())
            vk_id = int(vk_value.strip())
        except ValueError:
            continue
        mapping[tg_id] = vk_id
    return mapping


CHAT_MAPPING = parse_chat_mapping(os.getenv('CHAT_MAPPING', ''))
VK_TO_TG = {vk: tg for tg, vk in CHAT_MAPPING.items()}
SEEN_TG_MESSAGE_IDS = {}
SEEN_VK_MESSAGE_IDS = {}
MAX_SEEN_MESSAGE_IDS = 2000
DUPLICATE_TTL_SECONDS = 300

app = Flask(__name__)


def prune_seen_messages(store):
    now = time.time()
    expired_ids = [key for key, ts in store.items() if now - ts > DUPLICATE_TTL_SECONDS]
    for key in expired_ids:
        del store[key]

    if len(store) > MAX_SEEN_MESSAGE_IDS:
        oldest_ids = sorted(store, key=store.get)[:len(store) - MAX_SEEN_MESSAGE_IDS]
        for key in oldest_ids:
            del store[key]


def mark_seen_message(store, dedupe_key):
    if dedupe_key is None:
        return False

    prune_seen_messages(store)
    if dedupe_key in store:
        return True

    store[dedupe_key] = time.time()
    return False


def _raise_for_api_error(response, service_name):
    try:
        payload = response.json()
    except ValueError:
        payload = {}

    if response.status_code >= 400:
        raise RuntimeError(f'{service_name} HTTP {response.status_code}: {response.text[:200]}')
    if isinstance(payload, dict) and payload.get('error'):
        raise RuntimeError(f'{service_name} API error: {payload["error"]}')
    if isinstance(payload, dict) and payload.get('ok') is False:
        raise RuntimeError(f'{service_name} API error: {payload}')

    return payload


def require_bridge_config():
    missing = []
    if not TG_TOKEN:
        missing.append('TG_TOKEN')
    if not VK_TOKEN:
        missing.append('VK_TOKEN')
    if not CHAT_MAPPING:
        missing.append('CHAT_MAPPING')
    if missing:
        raise RuntimeError(f'Missing required config: {", ".join(missing)}')


def safe_dict_get(mapping, *path):
    current = mapping
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def send_vk_message(vk_user_id, text, attachment=''):
    payload = {
        'user_id': vk_user_id,
        'message': text,
        'attachment': attachment,
        'random_id': random.randint(-(2 ** 31), 2 ** 31 - 1),
        'from_group': 1,
        'access_token': VK_TOKEN,
        'v': '5.199',
    }
    response = requests.post(
        'https://api.vk.com/method/messages.send',
        data=payload,
        timeout=(5, 15),
    )
    data = _raise_for_api_error(response, 'VK messages.send')
    if data.get('response') is not None and data.get('response') != 1:
        raise RuntimeError(f'VK messages.send returned unexpected response: {data}')
    return True


@app.route('/tg_webhook', methods=['POST'])
def tg_webhook():
    try:
        require_bridge_config()
    except RuntimeError as exc:
        logger.warning(str(exc))
        return jsonify({'ok': False, 'error': 'bridge_not_configured'}), 503

    update = request.get_json(silent=True)
    if not isinstance(update, dict):
        return jsonify({'ok': True})
    if 'message' not in update:
        return jsonify({'ok': True})

    msg = update.get('message')
    if not isinstance(msg, dict):
        return jsonify({'ok': True})

    tg_chat_id = msg.get('chat', {}).get('id')
    if tg_chat_id is None or tg_chat_id not in CHAT_MAPPING:
        return jsonify({'ok': True})

    message_id = msg.get('message_id')
    dedupe_key = (tg_chat_id, message_id) if message_id is not None else None
    if mark_seen_message(SEEN_TG_MESSAGE_IDS, dedupe_key):
        return jsonify({'ok': True})

    vk_user_id = CHAT_MAPPING[tg_chat_id]
    text = msg.get('text') or msg.get('caption') or ''
    attachment = ''

    try:
        if 'photo' in msg:
            photo_list = msg.get('photo') or []
            if not photo_list:
                raise RuntimeError('Telegram photo payload is empty')

            file_id = photo_list[-1].get('file_id')
            if not file_id:
                raise RuntimeError('Missing Telegram file_id in photo payload')

            file_response = requests.get(
                f'https://api.telegram.org/bot{TG_TOKEN}/getFile',
                params={'file_id': file_id},
                timeout=(5, 15),
            )
            file_data = _raise_for_api_error(file_response, 'Telegram getFile')
            file_path = safe_dict_get(file_data, 'result', 'file_path')
            if not file_path:
                raise RuntimeError(f'Telegram getFile returned unexpected payload: {file_data}')

            img_response = requests.get(
                f'https://api.telegram.org/file/bot{TG_TOKEN}/{file_path}',
                timeout=(10, 30),
            )
            img_response.raise_for_status()
            img_data = img_response.content

            upload_server_response = requests.get(
                'https://api.vk.com/method/photos.getMessagesUploadServer',
                params={'peer_id': vk_user_id, 'access_token': VK_TOKEN, 'v': '5.199'},
                timeout=(5, 15),
            )
            upload_server_data = _raise_for_api_error(upload_server_response, 'VK getMessagesUploadServer')
            upload_url = safe_dict_get(upload_server_data, 'response', 'upload_url')
            if not upload_url:
                raise RuntimeError(f'VK upload server response missing upload_url: {upload_server_data}')

            upload_response = requests.post(
                upload_url,
                files={'photo': ('img.jpg', img_data, 'image/jpeg')},
                timeout=(10, 30),
            )
            upload_payload = _raise_for_api_error(upload_response, 'VK upload photo')

            save_response = requests.post(
                'https://api.vk.com/method/photos.saveMessagesPhoto',
                data={
                    'server': safe_dict_get(upload_payload, 'server'),
                    'photo': safe_dict_get(upload_payload, 'photo'),
                    'hash': safe_dict_get(upload_payload, 'hash'),
                    'access_token': VK_TOKEN,
                    'v': '5.199',
                },
                timeout=(5, 15),
            )
            save_data = _raise_for_api_error(save_response, 'VK saveMessagesPhoto')
            save_items = safe_dict_get(save_data, 'response')
            if not isinstance(save_items, list) or not save_items:
                raise RuntimeError(f'VK saveMessagesPhoto returned unexpected payload: {save_data}')
            save_item = save_items[0]
            attachment = f"photo{save_item['owner_id']}_{save_item['id']}"

        send_vk_message(vk_user_id, text, attachment)
    except Exception as exc:
        print(f'Ошибка TG->VK: {exc}')
        return jsonify({'ok': False, 'error': 'telegram_to_vk_failed'}), 500

    return jsonify({'ok': True})


@app.route('/vk_callback', methods=['POST'])
def vk_callback():
    try:
        require_bridge_config()
    except RuntimeError as exc:
        logger.warning(str(exc))
        return jsonify({'ok': False, 'error': 'bridge_not_configured'}), 503

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'ok': True})

    if data.get('type') == 'confirmation':
        return VK_CONFIRMATION

    try:
        if data.get('type') == 'message_new':
            msg = data.get('object', {}).get('message')
            if not isinstance(msg, dict):
                return jsonify({'ok': True})

            vk_user_id = msg.get('from_id')
            if vk_user_id is None or vk_user_id not in VK_TO_TG:
                return jsonify({'ok': True})

            message_id = msg.get('id') or data.get('event_id')
            dedupe_key = (vk_user_id, message_id) if message_id is not None else None
            if mark_seen_message(SEEN_VK_MESSAGE_IDS, dedupe_key):
                return jsonify({'ok': True})

            tg_chat_id = VK_TO_TG[vk_user_id]
            text = msg.get('text', '')
            photo_url = None
            if 'attachments' in msg:
                for att in msg.get('attachments', []):
                    if att.get('type') == 'photo':
                        photo_url = max(att['photo']['sizes'], key=lambda x: x['width'] * x['height'])['url']
                        break

            if photo_url:
                response = requests.post(
                    f'https://api.telegram.org/bot{TG_TOKEN}/sendPhoto',
                    json={
                        'chat_id': tg_chat_id,
                        'photo': photo_url,
                        'caption': text if text else None,
                    },
                    timeout=(5, 15),
                )
                _raise_for_api_error(response, 'Telegram sendPhoto')
            elif text:
                response = requests.post(
                    f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                    json={'chat_id': tg_chat_id, 'text': text},
                    timeout=(5, 15),
                )
                _raise_for_api_error(response, 'Telegram sendMessage')
    except Exception as exc:
        print(f'Ошибка VK->TG: {exc}')
        return jsonify({'ok': False, 'error': 'vk_to_telegram_failed'}), 500

    return jsonify({'ok': True})


@app.route('/healthz')
def healthz():
    configured = bool(TG_TOKEN and VK_TOKEN and CHAT_MAPPING)
    status_code = 200 if configured else 503
    return jsonify({
        'status': 'ok' if configured else 'misconfigured',
        'chat_pairs': len(CHAT_MAPPING),
        'configured': configured,
    }), status_code


@app.route('/')
def index():
    return '✅ Бот-мост работает!'


if __name__ == '__main__':
    print(f'🚀 Запуск. Привязано чатов: {len(CHAT_MAPPING)}')
    app.run(host='0.0.0.0', port=5000, debug=False)