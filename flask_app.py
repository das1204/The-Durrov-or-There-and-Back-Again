import hashlib
import hmac
import logging
import mimetypes
import os
import random
import requests
from flask import Flask, jsonify, request
from dotenv import load_dotenv
from urllib.parse import urljoin, urlparse
import storage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

TG_TOKEN = os.getenv('TG_TOKEN', '')
TG_OWNER_ID = os.getenv('TG_OWNER_ID', '')
TG_WEBHOOK_SECRET = os.getenv('TG_WEBHOOK_SECRET', '')
VK_TOKEN = os.getenv('VK_TOKEN', '')
VK_GROUP_ID = os.getenv('VK_GROUP_ID', '')
VK_CALLBACK_SECRET = os.getenv('VK_CALLBACK_SECRET', '')
VK_CONFIRMATION = os.getenv('VK_CONFIRMATION', '')
MAX_MESSAGE_LENGTH = 3500
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_MEDIA_BYTES = 20 * 1024 * 1024
_database_ready = False

app = Flask(__name__)


def _raise_for_api_error(response, service_name):
    try:
        payload = response.json()
    except ValueError:
        payload = {}

    if response.status_code >= 400:
        raise RuntimeError(f'{service_name} HTTP {response.status_code}')
    if isinstance(payload, dict) and payload.get('error'):
        error_code = safe_dict_get(payload, 'error', 'error_code')
        raise RuntimeError(f'{service_name} API error {error_code or "unknown"}')
    if isinstance(payload, dict) and payload.get('ok') is False:
        raise RuntimeError(f'{service_name} API returned ok=false')

    return payload


def require_bridge_config():
    missing = []
    for name, value in (
        ('TG_TOKEN', TG_TOKEN),
        ('TG_OWNER_ID', TG_OWNER_ID),
        ('TG_WEBHOOK_SECRET', TG_WEBHOOK_SECRET),
        ('VK_TOKEN', VK_TOKEN),
        ('VK_GROUP_ID', VK_GROUP_ID),
        ('VK_CALLBACK_SECRET', VK_CALLBACK_SECRET),
        ('VK_CONFIRMATION', VK_CONFIRMATION),
        ('DATABASE_URL', os.getenv('DATABASE_URL', '')),
    ):
        if not value:
            missing.append(name)
    if TG_OWNER_ID and (_owner_id() is None or _owner_id() <= 0):
        missing.append('TG_OWNER_ID must be a positive integer')
    if missing:
        raise RuntimeError(f'Missing required config: {", ".join(missing)}')


def safe_dict_get(mapping, *path):
    current = mapping
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def ensure_database():
    global _database_ready
    if not _database_ready:
        storage.initialize()
        _database_ready = True


def stable_vk_random_id(event_id):
    digest = hashlib.blake2s(str(event_id).encode('utf-8'), digest_size=4).digest()
    return int.from_bytes(digest, 'big') & 0x7fffffff


def send_vk_message(vk_user_id, text, attachment='', event_id=None):
    payload = {
        'user_id':      vk_user_id,
        'message':      text,
        'attachment':   attachment,
        'random_id':    stable_vk_random_id(event_id) if event_id is not None else random.randint(1, 2 ** 31 - 1),
        'from_group':   1,
        'access_token': VK_TOKEN,
        'v':            '5.199'
    }
    response = requests.post(
        'https://api.vk.com/method/messages.send', data=payload, timeout=(5, 15)
    )
    data = _raise_for_api_error(response, 'VK messages.send')
    sent_message_id = data.get('response') if isinstance(data, dict) else None
    if not isinstance(sent_message_id, int) or sent_message_id <= 0:
        raise RuntimeError('VK messages.send returned an invalid message id')
    return True


def _download_telegram_file(file_id, max_bytes):
    file_response = requests.get(
        f'https://api.telegram.org/bot{TG_TOKEN}/getFile',
        params={'file_id': file_id},
        timeout=(5, 15),
    )
    file_data = _raise_for_api_error(file_response, 'Telegram getFile')
    file_path = safe_dict_get(file_data, 'result', 'file_path')
    if not isinstance(file_path, str) or not file_path:
        raise RuntimeError('Telegram getFile returned an invalid file path')

    download_response = requests.get(
        f'https://api.telegram.org/file/bot{TG_TOKEN}/{file_path}',
        stream=True,
        timeout=(10, 30),
    )
    try:
        download_response.raise_for_status()
        chunks = []
        total_bytes = 0
        for chunk in download_response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total_bytes += len(chunk)
            if total_bytes > max_bytes:
                raise ValueError('Telegram media exceeds its size limit')
            chunks.append(chunk)
        content_type = download_response.headers.get('Content-Type', '').split(';', 1)[0]
        return b''.join(chunks), content_type
    finally:
        download_response.close()


def _safe_filename(filename, fallback):
    if not isinstance(filename, str):
        filename = ''
    filename = filename.replace('\\', '/').rsplit('/', 1)[-1].strip()
    filename = ''.join(character for character in filename if character.isprintable())
    return filename or fallback


def _is_vk_document_url(url):
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or '').lower()
    return (
        parsed.scheme == 'https'
        and port in (None, 443)
        and (
            host == 'vk.com'
            or host.endswith('.vk.com')
            or host == 'vk.ru'
            or host.endswith('.vk.ru')
            or host == 'userapi.com'
            or host.endswith('.userapi.com')
        )
    )


def _download_vk_document(url, max_bytes):
    current_url = url
    for _ in range(6):
        if not _is_vk_document_url(current_url):
            raise RuntimeError('VK document URL is invalid')
        response = requests.get(
            current_url,
            stream=True,
            allow_redirects=False,
            timeout=(10, 30),
        )
        try:
            if response.is_redirect:
                location = response.headers.get('Location')
                if not isinstance(location, str) or not location:
                    raise RuntimeError('VK document redirect has no location')
                current_url = urljoin(current_url, location)
                continue

            response.raise_for_status()
            chunks = []
            total_bytes = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise ValueError('VK document exceeds its size limit')
                chunks.append(chunk)
            content_type = response.headers.get('Content-Type', '').split(';', 1)[0]
            return b''.join(chunks), content_type
        finally:
            response.close()
    raise RuntimeError('VK document URL redirected too many times')


def _upload_vk_document(peer_id, file_data, filename, content_type):
    server_response = requests.get(
        'https://api.vk.com/method/docs.getMessagesUploadServer',
        params={'peer_id': peer_id, 'access_token': VK_TOKEN, 'v': '5.199'},
        timeout=(5, 15),
    )
    server_data = _raise_for_api_error(server_response, 'VK docs.getMessagesUploadServer')
    upload_url = safe_dict_get(server_data, 'response', 'upload_url')
    if not isinstance(upload_url, str) or not upload_url.startswith('https://'):
        raise RuntimeError('VK document upload server returned an invalid URL')

    upload_response = requests.post(
        upload_url,
        files={'file': (filename, file_data, content_type)},
        timeout=(10, 30),
    )
    upload_data = _raise_for_api_error(upload_response, 'VK document upload')
    upload_file = upload_data.get('file') if isinstance(upload_data, dict) else None
    if not isinstance(upload_file, str) or not upload_file:
        raise RuntimeError('VK document upload returned an invalid file')

    save_response = requests.post(
        'https://api.vk.com/method/docs.save',
        data={
            'file': upload_file,
            'title': filename[:255],
            'access_token': VK_TOKEN,
            'v': '5.199',
        },
        timeout=(5, 15),
    )
    save_data = _raise_for_api_error(save_response, 'VK docs.save')
    saved_doc = safe_dict_get(save_data, 'response', 'doc')
    if not isinstance(saved_doc, dict):
        raise RuntimeError('VK docs.save returned an invalid document')
    owner_id = saved_doc.get('owner_id')
    document_id = saved_doc.get('id')
    if not isinstance(owner_id, int) or not isinstance(document_id, int):
        raise RuntimeError('VK docs.save returned an invalid document')
    access_key = saved_doc.get('access_key')
    suffix = f'_{access_key}' if isinstance(access_key, str) and access_key else ''
    return f'doc{owner_id}_{document_id}{suffix}'


def _send_telegram_file(chat_id, method, field_name, file_data, filename, content_type, caption):
    data = {'chat_id': chat_id}
    if caption:
        data['caption'] = caption[:1024]
    response = requests.post(
        f'https://api.telegram.org/bot{TG_TOKEN}/{method}',
        data=data,
        files={field_name: (filename, file_data, content_type)},
        timeout=(10, 30),
    )
    _raise_for_api_error(response, f'Telegram {method}')


def send_telegram_message(chat_id, text, reply_markup=None):
    payload = {'chat_id': chat_id, 'text': text}
    if reply_markup is not None:
        payload['reply_markup'] = reply_markup
    response = requests.post(
        f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
        json=payload, timeout=(5, 15)
    )
    _raise_for_api_error(response, 'Telegram sendMessage')


def send_telegram_callback_answer(callback_id, text='', show_alert=False):
    response = requests.post(
        f'https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery',
        json={'callback_query_id': callback_id, 'text': text, 'show_alert': show_alert},
        timeout=(5, 15),
    )
    _raise_for_api_error(response, 'Telegram answerCallbackQuery')


def _command(text):
    if not isinstance(text, str) or not text.strip():
        return ''
    return text.strip().split(maxsplit=1)[0].split('@', 1)[0].lower()


def _owner_id():
    try:
        return int(TG_OWNER_ID)
    except (TypeError, ValueError):
        return None


def _handle_telegram_callback(callback):
    callback_id = callback.get('id')
    actor_id = safe_dict_get(callback, 'from', 'id')
    chat_id = safe_dict_get(callback, 'message', 'chat', 'id')
    if actor_id != _owner_id() or chat_id != _owner_id():
        return
    callback_data = callback.get('data', '')
    if not isinstance(callback_data, str) or not callback_data.startswith('select:'):
        if callback_id:
            send_telegram_callback_answer(callback_id, 'Неизвестная команда.', True)
        return
    try:
        vk_user_id = int(callback_data.partition(':')[2])
    except ValueError:
        if callback_id:
            send_telegram_callback_answer(callback_id, 'Этот контакт недоступен.', True)
        return
    if not storage.select_contact(_owner_id(), vk_user_id):
        if callback_id:
            send_telegram_callback_answer(callback_id, 'Согласие отозвано или контакт недоступен.', True)
        send_telegram_message(chat_id, 'Этот VK-пользователь больше недоступен. Откройте /list и выберите другого.')
        return
    contact = next(
        (item for item in storage.list_consented_contacts() if item['vk_user_id'] == vk_user_id),
        None,
    )
    name = contact['display_name'] if contact else 'VK-пользователь'
    if callback_id:
        send_telegram_callback_answer(callback_id, f'Выбран: {name}')
    send_telegram_message(chat_id, f'Выбран собеседник: {name}. Команда /stop завершит диалог.')


def _handle_telegram_message(message, event_id):
    chat = message.get('chat')
    sender = message.get('from')
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return
    owner_id = _owner_id()
    if chat.get('type') != 'private' or chat.get('id') != owner_id or sender.get('id') != owner_id:
        return

    text = message.get('text') or message.get('caption') or ''
    command = _command(message.get('text'))
    if command in {'/start', '/help'}:
        send_telegram_message(owner_id, 'Используйте /list для выбора VK-собеседника и /stop для завершения диалога.')
        return
    if command == '/list':
        contacts = storage.list_consented_contacts()
        if not contacts:
            send_telegram_message(owner_id, 'Пока нет согласившихся VK-пользователей. Они должны отправить сообществу /connect.')
            return
        keyboard = [
            [{'text': item['display_name'], 'callback_data': f"select:{item['vk_user_id']}"}] for item in contacts[:90]
        ]
        send_telegram_message(owner_id, 'Выберите VK-собеседника:', {'inline_keyboard': keyboard})
        return
    if command == '/stop':
        storage.clear_selection(owner_id)
        send_telegram_message(owner_id, 'Диалог завершён. Чтобы выбрать собеседника, используйте /list.')
        return

    document = message.get('document')
    document_name = document.get('file_name', '') if isinstance(document, dict) else ''
    document_mime = document.get('mime_type', '') if isinstance(document, dict) else ''
    is_gif_document = (
        isinstance(document_name, str) and document_name.lower().endswith('.gif')
    ) or document_mime == 'image/gif'
    unsupported_fields = {
        'audio', 'voice', 'video_note', 'sticker', 'contact', 'location',
        'venue', 'poll', 'dice',
    }
    if unsupported_fields.intersection(message):
        send_telegram_message(owner_id, 'Поддерживаются текст, фотографии, видео, GIF и документы.')
        return

    selected = storage.get_selected_contact(owner_id)
    if selected is None:
        send_telegram_message(owner_id, 'Сначала выберите собеседника командой /list.')
        return
    if len(text) > MAX_MESSAGE_LENGTH:
        send_telegram_message(owner_id, 'Сообщение слишком длинное. Максимум 3500 символов.')
        return
    if not storage.reserve_outbound_message(owner_id):
        send_telegram_message(owner_id, 'Достигнут лимит сообщений: не более 10 в минуту и 1000 в сутки.')
        return

    attachment = ''
    photos = message.get('photo')
    if photos is not None:
        if not isinstance(photos, list) or not photos or not isinstance(photos[-1], dict):
            send_telegram_message(owner_id, 'Не удалось обработать фотографию.')
            return
        file_id = photos[-1].get('file_id')
        if not isinstance(file_id, str) or not file_id:
            send_telegram_message(owner_id, 'Не удалось обработать фотографию.')
            return
        file_response = requests.get(
            f'https://api.telegram.org/bot{TG_TOKEN}/getFile',
            params={'file_id': file_id}, timeout=(5, 15)
        )
        file_data = _raise_for_api_error(file_response, 'Telegram getFile')
        file_path = safe_dict_get(file_data, 'result', 'file_path')
        if not isinstance(file_path, str) or not file_path:
            raise RuntimeError('Telegram getFile returned an invalid file path')
        image_response = requests.get(
            f'https://api.telegram.org/file/bot{TG_TOKEN}/{file_path}', timeout=(10, 30)
        )
        image_response.raise_for_status()
        image_data = image_response.content
        if len(image_data) > MAX_PHOTO_BYTES:
            send_telegram_message(owner_id, 'Фотография превышает лимит 10 МБ.')
            return
        if not image_content_type.startswith('image/'):
            send_telegram_message(owner_id, 'Поддерживаются только фотографии.')
            return

        upload_server_response = requests.get(
            'https://api.vk.com/method/photos.getMessagesUploadServer',
            params={'peer_id': selected['vk_user_id'], 'access_token': VK_TOKEN, 'v': '5.199'},
            timeout=(5, 15),
        )
        upload_server_data = _raise_for_api_error(upload_server_response, 'VK getMessagesUploadServer')
        upload_url = safe_dict_get(upload_server_data, 'response', 'upload_url')
        if not isinstance(upload_url, str) or not upload_url.startswith('https://'):
            raise RuntimeError('VK upload server returned an invalid URL')
        upload_response = requests.post(
            upload_url,
            files={'photo': ('image', image_data, image_content_type)},
            timeout=(10, 30),
        )
        upload_payload = _raise_for_api_error(upload_response, 'VK upload photo')
        save_response = requests.post(
            'https://api.vk.com/method/photos.saveMessagesPhoto',
            data={
                'server':       safe_dict_get(upload_payload, 'server'),
                'photo':        safe_dict_get(upload_payload, 'photo'),
                'hash':         safe_dict_get(upload_payload, 'hash'),
                'access_token': VK_TOKEN,
                'v':            '5.199'
            },
            timeout=(5, 15),
        )
        save_data = _raise_for_api_error(save_response, 'VK saveMessagesPhoto')
        save_items = safe_dict_get(save_data, 'response')
        if not isinstance(save_items, list) or not save_items or not isinstance(save_items[0], dict):
            raise RuntimeError('VK saveMessagesPhoto returned an invalid response')
        save_item = save_items[0]
        owner_id = save_item.get('owner_id')
        photo_id = save_item.get('id')
        if not isinstance(owner_id, int) or not isinstance(photo_id, int):
            raise RuntimeError('VK saveMessagesPhoto returned an invalid photo')
        attachment = f'photo{owner_id}_{photo_id}'

    media_message = None
    media_kind = None
    if isinstance(message.get('video'), dict):
        media_message = message['video']
        media_kind = 'video'
    elif isinstance(message.get('animation'), dict):
        media_message = message['animation']
        media_kind = 'animation'
    elif is_gif_document and isinstance(document, dict):
        media_message = document
        media_kind = 'gif'
    elif isinstance(document, dict):
        media_message = document
        media_kind = 'document'

    if media_message is not None:
        file_id = media_message.get('file_id')
        if not isinstance(file_id, str) or not file_id:
            send_telegram_message(owner_id, 'Не удалось обработать файл.')
            return
        try:
            file_data, downloaded_content_type = _download_telegram_file(
                file_id, MAX_MEDIA_BYTES
            )
        except ValueError:
            send_telegram_message(owner_id, 'Файл превышает лимит 20 МБ.')
            return
        filename = media_message.get('file_name')
        if not isinstance(filename, str) or not filename.strip():
            filename = {
                'video': 'video.mp4',
                'animation': 'animation.mp4',
                'gif': 'animation.gif',
                'document': 'document',
            }[media_kind]
        filename = _safe_filename(filename, 'document')
        content_type = media_message.get('mime_type')
        if not isinstance(content_type, str) or '/' not in content_type:
            content_type = downloaded_content_type or mimetypes.guess_type(filename)[0] or 'application/octet-stream'

        attachment = _upload_vk_document(
            selected['vk_user_id'], file_data, filename, content_type
        )

    if not text and not attachment:
        send_telegram_message(owner_id, 'Поддерживаются текст, фотографии, видео, GIF и документы.')
        return
    current_selection = storage.get_selected_contact(owner_id)
    if current_selection is None or current_selection['vk_user_id'] != selected['vk_user_id']:
        send_telegram_message(owner_id, 'Согласие или выбор собеседника изменились. Выберите контакт заново через /list.')
        return
    send_vk_message(selected['vk_user_id'], text, attachment, event_id=f'tg:{event_id}')


def _handle_telegram_update(update, event_id):
    callback = update.get('callback_query')
    if isinstance(callback, dict):
        _handle_telegram_callback(callback)
        return
    message = update.get('message')
    if isinstance(message, dict):
        _handle_telegram_message(message, event_id)


def _verify_telegram_webhook():
    provided = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    return bool(TG_WEBHOOK_SECRET) and hmac.compare_digest(provided, TG_WEBHOOK_SECRET)


@app.route('/tg_webhook', methods=['POST'])
def tg_webhook():
    if not _verify_telegram_webhook():
        return jsonify({'ok': False, 'error': 'unauthorized'}), 403
    try:
        require_bridge_config()
    except RuntimeError as exc:
        logger.warning(str(exc))
        return jsonify({'ok': False, 'error': 'bridge_not_configured'}), 503

    update = request.get_json(silent=True)
    if not isinstance(update, dict) or not isinstance(update.get('update_id'), int):
        return jsonify({'ok': False, 'error': 'invalid_payload'}), 400
    event_id = str(update['update_id'])
    try:
        ensure_database()
        event_claim = storage.claim_event('telegram', event_id)
        if event_claim is False:
            return jsonify({'ok': True})
        if event_claim is None:
            return jsonify({'ok': False, 'error': 'event_in_progress'}), 503
        _handle_telegram_update(update, event_id)
        storage.finish_event('telegram', event_id, 'sent')
    except Exception:
        try:
            storage.finish_event('telegram', event_id, 'failed')
        except Exception:
            logger.exception(
                'Failed to mark Telegram webhook event %s as failed',
                event_id,
            )
        logger.exception('Telegram webhook processing failed (event_id=%s)', event_id)
        return jsonify({'ok': False, 'error': 'telegram_to_vk_failed'}), 500
    return jsonify({'ok': True})


def _vk_display_name(vk_user_id):
    try:
        response = requests.get(
            'https://api.vk.com/method/users.get',
            params={'user_ids': vk_user_id, 'access_token': VK_TOKEN, 'v': '5.199'},
            timeout=(5, 15)
        )
        data = _raise_for_api_error(response, 'VK users.get')
        users = safe_dict_get(data, 'response')
        if isinstance(users, list) and users and isinstance(users[0], dict):
            name = ' '.join(part for part in (users[0].get('first_name'), users[0].get('last_name')) if isinstance(part, str)).strip()
            if name:
                return name[:80]
    except Exception:
        pass
    return 'Пользователь VK'


def _vk_send_id(event_id, suffix='reply'):
    return f'vk:{event_id}:{suffix}'


def _handle_vk_message(message, event_id):
    vk_user_id = message.get('from_id')
    if not isinstance(vk_user_id, int) or vk_user_id <= 0:
        return
    text = message.get('text', '')
    if not isinstance(text, str):
        text = ''
    command = _command(text)

    if command == '/connect':
        storage.register_consent(vk_user_id, _vk_display_name(vk_user_id))
        send_vk_message(
            vk_user_id,
            'Согласие сохранено. Вы можете получать сообщения от владельца Telegram-моста. Для отзыва согласия отправьте /disconnect.',
            event_id=_vk_send_id(event_id, 'connect')
        )
        return
    if command == '/disconnect':
        storage.revoke_consent(vk_user_id)
        send_vk_message(
            vk_user_id,
            'Согласие отозвано. Новые сообщения через мост пересылаться не будут.',
            event_id=_vk_send_id(event_id, 'disconnect')
        )
        return
    if command in {'/start', '/help'}:
        send_vk_message(
            vk_user_id,
            'Чтобы разрешить сообщения через мост, отправьте /connect. Для немедленного прекращения и отзыва согласия отправьте /disconnect.',
            event_id=_vk_send_id(event_id, 'help')
        )
        return

    selected = storage.get_selected_contact(_owner_id())
    if selected is None or selected['vk_user_id'] != vk_user_id:
        return

    photo_url = None
    animation_file = None
    document_file = None
    video_link = None
    attachments = message.get('attachments', [])
    if isinstance(attachments, list):
        for item in attachments:
            if not isinstance(item, dict):
                continue
            attachment_type = item.get('type')
            if attachment_type == 'photo':
                sizes = safe_dict_get(item, 'photo', 'sizes')
                if not isinstance(sizes, list):
                    continue
                valid_sizes = [size for size in sizes if isinstance(size, dict) and isinstance(size.get('url'), str)]
                if valid_sizes:
                    photo_url = max(valid_sizes, key=lambda size: (size.get('width', 0) or 0) * (size.get('height', 0) or 0))['url']
                break
            if attachment_type == 'video':
                video = item.get('video')
                owner_id = video.get('owner_id') if isinstance(video, dict) else None
                video_id = video.get('id') if isinstance(video, dict) else None
                if isinstance(owner_id, int) and isinstance(video_id, int):
                    video_link = f'https://vk.com/video{owner_id}_{video_id}'
                    access_key = video.get('access_key')
                    if isinstance(access_key, str) and access_key and all(
                        character.isalnum() or character in '_-' for character in access_key
                    ):
                        video_link += f'?access_key={access_key}'
                    break
                send_telegram_message(_owner_id(), 'Не удалось получить ссылку на видео VK.')
                return
            if attachment_type == 'doc':
                document = item.get('doc')
                document_url = document.get('url') if isinstance(document, dict) else None
                if not _is_vk_document_url(document_url):
                    send_telegram_message(_owner_id(), 'Не удалось получить документ из VK.')
                    return
                extension = document.get('ext', '') if isinstance(document, dict) else ''
                title = document.get('title', '') if isinstance(document, dict) else ''
                if isinstance(extension, str):
                    extension = ''.join(character for character in extension if character.isalnum())[:16]
                else:
                    extension = ''
                filename = _safe_filename(
                    title, f'document.{extension}' if extension else 'document'
                )
                if extension and not filename.lower().endswith(f'.{extension.lower()}'):
                    filename = f'{filename}.{extension}'
                file_info = (document_url, filename)
                if isinstance(extension, str) and extension.lower() == 'gif':
                    animation_file = file_info
                else:
                    document_file = file_info
                break
            send_telegram_message(_owner_id(), 'Получено неподдерживаемое вложение VK; пересылаются текст, фотографии, видео, GIF и документы.')
            return

    if video_link:
        text = f'{text}\n{video_link}' if text else video_link
    if not text and not photo_url and not animation_file and not document_file:
        return
    if not storage.reserve_outbound_message(_owner_id()):
        logger.warning('Bridge message rate limit reached')
        return
    current_selection = storage.get_selected_contact(_owner_id())
    if current_selection is None or current_selection['vk_user_id'] != vk_user_id:
        return

    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[:MAX_MESSAGE_LENGTH]
    if photo_url:
        response = requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendPhoto',
            json={'chat_id': _owner_id(), 'photo': photo_url, 'caption': text[:1024] if text else None},
            timeout=(5, 15)
        )
        _raise_for_api_error(response, 'Telegram sendPhoto')
        if len(text) > 1024:
            send_telegram_message(_owner_id(), text[1024:])
    elif animation_file:
        document_url, filename = animation_file
        try:
            file_data, content_type = _download_vk_document(document_url, MAX_MEDIA_BYTES)
        except ValueError:
            send_telegram_message(_owner_id(), 'GIF из VK превышает лимит 20 МБ.')
            return
        content_type = content_type or mimetypes.guess_type(filename)[0] or 'image/gif'
        _send_telegram_file(
            _owner_id(), 'sendAnimation', 'animation', file_data, filename,
            content_type, text,
        )
        if len(text) > 1024:
            send_telegram_message(_owner_id(), text[1024:])
    elif document_file:
        document_url, filename = document_file
        try:
            file_data, content_type = _download_vk_document(document_url, MAX_MEDIA_BYTES)
        except ValueError:
            send_telegram_message(_owner_id(), 'Документ из VK превышает лимит 20 МБ.')
            return
        content_type = content_type or mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        _send_telegram_file(
            _owner_id(), 'sendDocument', 'document', file_data, filename,
            content_type, text,
        )
        if len(text) > 1024:
            send_telegram_message(_owner_id(), text[1024:])
    elif text:
        send_telegram_message(_owner_id(), text)


@app.route('/vk_callback', methods=['POST'])
def vk_callback():
    try:
        require_bridge_config()
    except RuntimeError as exc:
        logger.warning(str(exc))
        return jsonify({'ok': False, 'error': 'bridge_not_configured'}), 503

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'invalid_payload'}), 400
    provided_secret = data.get('secret', '')
    if not isinstance(provided_secret, str) or not hmac.compare_digest(provided_secret, VK_CALLBACK_SECRET):
        return jsonify({'ok': False, 'error': 'unauthorized'}), 403

    if data.get('type') == 'confirmation':
        return VK_CONFIRMATION

    if data.get('type') != 'message_new':
        return 'ok'
    event_id = data.get('event_id')
    if not isinstance(event_id, str) or not event_id:
        return jsonify({'ok': False, 'error': 'invalid_payload'}), 400
    message = safe_dict_get(data, 'object', 'message')
    if not isinstance(message, dict):
        return jsonify({'ok': False, 'error': 'invalid_payload'}), 400

    try:
        ensure_database()
        event_claim = storage.claim_event('vk', event_id)
        if event_claim is False:
            return 'ok'
        if event_claim is None:
            return 'retry', 503
        _handle_vk_message(message, event_id)
        storage.finish_event('vk', event_id, 'sent')
    except Exception:
        try:
            storage.finish_event('vk', event_id, 'failed')
        except Exception:
            pass
        logger.error('VK webhook processing failed')
        return jsonify({'ok': False, 'error': 'vk_to_telegram_failed'}), 500
    return 'ok'


@app.route('/healthz')
def healthz():
    try:
        require_bridge_config()
        configured = True
    except Exception:
        configured = False
    status_code = 200 if configured else 503
    return jsonify({
        'status':     'ok' if configured else 'misconfigured',
        'configured': configured,
    }), status_code


@app.route('/readyz')
def readyz():
    try:
        require_bridge_config()
        ensure_database()
        with storage.connect() as connection:
            connection.execute('SELECT 1')
        database_ready = True
    except Exception:
        database_ready = False
    status_code = 200 if database_ready else 503
    return jsonify({
        'status':         'ready' if database_ready else 'not_ready',
        'database_ready': database_ready,
    }), status_code


@app.route('/')
def index():
    return '✅ Бот-мост работает!'


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)