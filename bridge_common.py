import hashlib
import logging
import os
import random
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from dotenv import load_dotenv

import storage

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

load_dotenv()

TG_TOKEN = os.getenv('TG_TOKEN', '')
TG_OWNER_ID = os.getenv('TG_OWNER_ID', '')
TG_FORUM_CHAT_ID = os.getenv('TG_FORUM_CHAT_ID', '')
TG_WEBHOOK_SECRET = os.getenv('TG_WEBHOOK_SECRET', '')
VK_TOKEN = os.getenv('VK_TOKEN', '')
VK_GROUP_ID = os.getenv('VK_GROUP_ID', '')
VK_CALLBACK_SECRET = os.getenv('VK_CALLBACK_SECRET', '')
VK_CONFIRMATION = os.getenv('VK_CONFIRMATION', '')
DATABASE_URL = os.getenv('DATABASE_URL', '')
MAX_MESSAGE_LENGTH = 3500
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_MEDIA_BYTES = 20 * 1024 * 1024
VIDEO_UNAVAILABLE_NOTICE = 'Отправка видео доступна только в полной версии.'
VIDEO_EXTENSIONS = {
    '.3gp', '.avi', '.flv', '.m4v', '.mkv', '.mov', '.mp4', '.mpeg', '.mpg', '.webm', '.wmv'
}

_database_ready = False
_errors_topic_lock = threading.Lock()
_errors_topic_id = None
_error_context = threading.local()

__all__ = [
    'DATABASE_URL', 'MAX_MEDIA_BYTES', 'MAX_MESSAGE_LENGTH', 'MAX_PHOTO_BYTES',
    'TG_FORUM_CHAT_ID', 'TG_OWNER_ID', 'TG_TOKEN', 'TG_WEBHOOK_SECRET',
    'VIDEO_UNAVAILABLE_NOTICE', 'VK_CALLBACK_SECRET', 'VK_CONFIRMATION', 'VK_GROUP_ID',
    'VK_TOKEN', '_command', '_download_telegram_file', '_download_vk_document',
    '_download_vk_file', '_ensure_errors_topic', '_error_context', '_forum_chat_id',
    '_is_video_file', '_is_vk_document_url', '_is_vk_photo_url', '_log_exception',
    '_owner_id', '_raise_for_api_error', '_redact_secrets', '_request', '_safe_filename',
    '_send_telegram_file', '_telegram_forward_source', '_upload_vk_document',
    '_upload_vk_message_photo', 'create_telegram_forum_topic', 'ensure_database',
    'logger', 'require_bridge_config', 'safe_dict_get', 'send_telegram_callback_answer',
    'send_telegram_message', 'send_vk_message', 'stable_vk_random_id', 'storage',
]


def _redact_secrets(value):
    for secret in (TG_TOKEN, VK_TOKEN, TG_WEBHOOK_SECRET, VK_CALLBACK_SECRET, DATABASE_URL):
        if secret:
            value = value.replace(secret, '[REDACTED]')
    return re.sub(r'(https?://[^\s?#]+)\?[^\s#]*', r'\1?[REDACTED]', value)

def _raise_for_api_error(response, service_name):
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if response.status_code >= 400:
        error_code = None
        description = None
        if isinstance(payload, dict):
            error_code = payload.get('error_code')
            description = payload.get('description')
            error = payload.get('error')
            if isinstance(error, dict):
                error_code = error.get('error_code', error_code)
                description = error.get('error_msg', description)
        details = []
        if error_code is not None:
            details.append(f'API error {error_code}')
        if isinstance(description, str) and description.strip():
            description = _redact_secrets(' '.join(description.split())[:300])
            details.append(description)
        suffix = f' ({": ".join(details)})' if details else ''
        error_message = f'{service_name} HTTP {response.status_code}{suffix}'
        logger.error('%s', error_message)
        raise RuntimeError(error_message)
    if not isinstance(payload, dict):
        error_message = (
            f'{service_name} returned invalid JSON or a non-object response '
            f'(HTTP {response.status_code})'
        )
        logger.error('%s', error_message)
        raise RuntimeError(error_message)
    if isinstance(payload, dict) and payload.get('error'):
        error_code = safe_dict_get(payload, 'error', 'error_code')
        error_message = safe_dict_get(payload, 'error', 'error_msg')
        details = (
            f': {_redact_secrets(" ".join(error_message.split())[:300])}'
            if isinstance(error_message, str) and error_message.strip() else ''
        )
        error_message = (f'{service_name} API error {error_code or "unknown"}{details}')
        logger.error('%s', error_message)
        raise RuntimeError(error_message)
    if isinstance(payload, dict) and payload.get('ok') is False:
        error_message = f'{service_name} API returned ok=false'
        logger.error('%s', error_message)
        raise RuntimeError(error_message)

    return payload

def _log_exception(message, *args, exception=None):
    formatted_traceback = _redact_secrets(traceback.format_exc())
    log_message = message % args if args else message
    logger.error('%s\n%s', log_message, formatted_traceback.rstrip())
    exception = exception or sys.exc_info()[1]
    exception_type = type(exception).__name__ if exception else 'UnknownError'
    error_details = str(exception) if exception else log_message
    _report_bot_error(exception_type, error_details)

def _report_bot_error(error_type, error_details):
    if getattr(_error_context, 'reporting', False):
        return
    topic_id = _errors_topic_id
    if topic_id is None:
        return
    chat_name = getattr(_error_context, 'chat', None)
    if not isinstance(chat_name, str) or not chat_name.strip():
        chat_name = None
    else:
        chat_name = chat_name.strip()
    description = _redact_secrets(' '.join(str(error_details).split()))[:1200]
    chat_description = f' в чате с {chat_name}' if chat_name else ''
    report = (
        f'{datetime.now(timezone.utc).isoformat(timespec="seconds")} Ошибка{chat_description}\n'
        f'Тип: {error_type}\n'
        f'Описание: {description}'
    )
    _error_context.reporting = True
    try:
        send_telegram_message(_forum_chat_id(), report, message_thread_id=topic_id)
    except Exception as reporting_error:
        logger.error(
            'Failed to send bot error report: %s: %s',
            type(reporting_error).__name__,
            _redact_secrets(str(reporting_error))
        )
    finally:
        _error_context.reporting = False

def _ensure_errors_topic():
    global _errors_topic_id
    if _errors_topic_id is not None:
        return _errors_topic_id
    with _errors_topic_lock:
        if _errors_topic_id is not None:
            return _errors_topic_id
        configured_topic_id = storage.get_setting('errors_topic_id')
        if configured_topic_id is not None:
            try:
                _errors_topic_id = int(configured_topic_id)
            except ValueError as exc:
                raise RuntimeError('Stored Errors forum topic id is invalid') from exc
            if _errors_topic_id <= 0:
                raise RuntimeError('Stored Errors forum topic id must be positive')
            return _errors_topic_id
        topic_id = create_telegram_forum_topic('Errors')
        storage.save_setting('errors_topic_id', topic_id)
        _errors_topic_id = topic_id
        logger.info('Created Errors Telegram forum topic: message_thread_id=%s', topic_id)
        return topic_id

def _request(service_name, method, url, **kwargs):
    try:
        return requests.request(method, url, **kwargs)
    except requests.RequestException:
        _log_exception('HTTP request failed: service=%s method=%s', service_name, method.upper())
        raise

def require_bridge_config():
    missing = []
    for name, value in (
        ('TG_TOKEN',           TG_TOKEN),
        ('TG_OWNER_ID',        TG_OWNER_ID),
        ('TG_FORUM_CHAT_ID',   TG_FORUM_CHAT_ID),
        ('TG_WEBHOOK_SECRET',  TG_WEBHOOK_SECRET),
        ('VK_TOKEN',           VK_TOKEN),
        ('VK_GROUP_ID',        VK_GROUP_ID),
        ('VK_CALLBACK_SECRET', VK_CALLBACK_SECRET),
        ('VK_CONFIRMATION',    VK_CONFIRMATION),
        ('DATABASE_URL',       DATABASE_URL)
    ):
        if not value:
            missing.append(name)
    if TG_OWNER_ID and (_owner_id() is None or _owner_id() <= 0):
        missing.append('TG_OWNER_ID must be a positive integer')
    if TG_FORUM_CHAT_ID and _forum_chat_id() is None:
        missing.append('TG_FORUM_CHAT_ID must be a negative integer')
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
        try:
            storage.initialize()
            _database_ready = True
            logger.info('Database schema initialization completed')
        except Exception:
            _log_exception('Database schema initialization failed')
            raise

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
    response = _request(
        'VK messages.send', 'POST', 'https://api.vk.com/method/messages.send',
        data=payload, timeout=(5, 15)
    )
    data = _raise_for_api_error(response, 'VK messages.send')
    sent_message_id = data.get('response') if isinstance(data, dict) else None
    if not isinstance(sent_message_id, int) or sent_message_id <= 0:
        raise RuntimeError('VK messages.send returned an invalid message id')
    logger.info(
        'VK message sent: vk_user_id=%s event_id=%s message_id=%s attachment_count=%s',
        vk_user_id, event_id or 'none', sent_message_id, len(attachment.split(',')) if attachment else 0
    )
    return True

def _download_telegram_file(file_id, max_bytes):
    file_response = _request(
        'Telegram getFile', 'GET', f'https://api.telegram.org/bot{TG_TOKEN}/getFile',
        params={'file_id': file_id}, timeout=(5, 15)
    )
    file_data = _raise_for_api_error(file_response, 'Telegram getFile')
    file_path = safe_dict_get(file_data, 'result', 'file_path')
    if not isinstance(file_path, str) or not file_path:
        raise RuntimeError('Telegram getFile returned an invalid file path')

    download_response = _request(
        'Telegram file download', 'GET', f'https://api.telegram.org/file/bot{TG_TOKEN}/{file_path}',
        stream=True, timeout=(10, 30)
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
    try:
        parsed = urlparse(url)
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
            or host == 'vkuser.net'
            or host.endswith('.vkuser.net')
            or host == 'vkusercdn.ru'
            or host.endswith('.vkusercdn.ru')
            or host == 'vkuserphoto.ru'
            or host.endswith('.vkuserphoto.ru')
        )
    )

def _is_vk_photo_url(url):
    if _is_vk_document_url(url):
        return True
    if not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or '').lower()
    return (
        parsed.scheme == 'https'
        and port in (None, 443)
        and (host == 'vkuserphoto.ru' or host.endswith('.vkuserphoto.ru'))
    )

def _download_vk_file(url, max_bytes, allow_photo_cdn=False):
    current_url = url
    for _ in range(6):
        is_allowed_url = (
            _is_vk_photo_url(current_url)
            if allow_photo_cdn else _is_vk_document_url(current_url)
        )
        if not is_allowed_url:
            host = urlparse(current_url).hostname or 'unknown'
            raise RuntimeError(f'VK media URL is invalid: host={host}')
        response = _request(
            'VK media download', 'GET', current_url, stream=True, allow_redirects=False, timeout=(10, 30)
        )
        try:
            if response.is_redirect:
                location = response.headers.get('Location')
                if not isinstance(location, str) or not location:
                    raise RuntimeError('VK media redirect has no location')
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
                    raise ValueError('VK media exceeds its size limit')
                chunks.append(chunk)
            content_type = response.headers.get('Content-Type', '').split(';', 1)[0]
            return b''.join(chunks), content_type
        finally:
            response.close()
    raise RuntimeError('VK media URL redirected too many times')

def _download_vk_document(url, max_bytes):
    return _download_vk_file(url, max_bytes)

def _upload_vk_document(peer_id, file_data, filename, content_type):
    server_response = _request(
        'VK docs.getMessagesUploadServer', 'GET', 'https://api.vk.com/method/docs.getMessagesUploadServer',
        params={'peer_id': peer_id, 'access_token': VK_TOKEN, 'v': '5.199'}, timeout=(5, 15)
    )
    server_data = _raise_for_api_error(server_response, 'VK docs.getMessagesUploadServer')
    upload_url = safe_dict_get(server_data, 'response', 'upload_url')
    if not isinstance(upload_url, str) or not upload_url.startswith('https://'):
        raise RuntimeError('VK document upload server returned an invalid URL')

    upload_response = _request(
        'VK document upload', 'POST', upload_url,
        files={'file': (filename, file_data, content_type)}, timeout=(10, 30)
    )
    upload_data = _raise_for_api_error(upload_response, 'VK document upload')
    upload_file = upload_data.get('file') if isinstance(upload_data, dict) else None
    if not isinstance(upload_file, str) or not upload_file:
        raise RuntimeError('VK document upload returned an invalid file')

    save_response = _request(
        'VK docs.save', 'POST', 'https://api.vk.com/method/docs.save',
        data={
            'file':         upload_file,
            'title':        filename[:255],
            'access_token': VK_TOKEN,
            'v':            '5.199'
        }, timeout=(5, 15)
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

def _upload_vk_message_photo(peer_id, image_data, image_content_type):
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        upload_server_response = _request(
            'VK photos.getMessagesUploadServer', 'GET', 'https://api.vk.com/method/photos.getMessagesUploadServer',
            params={
                'peer_id':      peer_id,
                'access_token': VK_TOKEN,
                'v':            '5.199'
                }, timeout=(5, 15)
        )
        upload_server_data = _raise_for_api_error(upload_server_response, 'VK getMessagesUploadServer')
        upload_url = safe_dict_get(upload_server_data, 'response', 'upload_url')
        if not isinstance(upload_url, str) or not upload_url.startswith('https://'):
            raise RuntimeError('VK upload server returned an invalid URL')
        upload_response = _request(
            'VK photo upload', 'POST', upload_url,
            files={'photo': ('image.jpg', image_data, image_content_type)}, timeout=(10, 30)
        )
        upload_payload = _raise_for_api_error(upload_response, 'VK upload photo')
        server = safe_dict_get(upload_payload, 'server')
        photo = safe_dict_get(upload_payload, 'photo')
        photo_hash = safe_dict_get(upload_payload, 'hash')
        if server is None or not isinstance(photo, str) or not photo or not photo_hash:
            raise RuntimeError('VK photo upload returned an invalid upload receipt')

        save_response = _request(
            'VK photos.saveMessagesPhoto', 'POST',
            'https://api.vk.com/method/photos.saveMessagesPhoto',
            data={
                'server':       server,
                'photo':        photo,
                'hash':         photo_hash,
                'access_token': VK_TOKEN,
                'v':            '5.199'
            }, timeout=(5, 15)
        )
        try:
            save_data = _raise_for_api_error(save_response, 'VK saveMessagesPhoto')
        except RuntimeError as exc:
            if 'photos_list is invalid' not in str(exc) or attempt == max_attempts:
                raise
            logger.warning(
                'VK rejected uploaded photo receipt; retrying upload: peer_id=%s attempt=%s/%s',
                peer_id, attempt + 1, max_attempts
            )
            time.sleep(attempt)
            continue

        save_items = safe_dict_get(save_data, 'response')
        if not isinstance(save_items, list) or not save_items or not isinstance(save_items[0], dict):
            raise RuntimeError('VK saveMessagesPhoto returned an invalid response')
        save_item = save_items[0]
        owner_id = save_item.get('owner_id')
        photo_id = save_item.get('id')
        if not isinstance(owner_id, int) or not isinstance(photo_id, int):
            raise RuntimeError('VK saveMessagesPhoto returned an invalid photo')
        access_key = save_item.get('access_key')
        suffix = (
            f'_{access_key}'
            if isinstance(access_key, str) and access_key
            and all(character.isalnum() or character in '_-' for character in access_key)
            else ''
        )
        return f'photo{owner_id}_{photo_id}{suffix}'

    raise RuntimeError('VK photo upload retries were exhausted')

def _send_telegram_file(chat_id, method, field_name, file_data, filename, content_type, caption, message_thread_id=None):
    data = {'chat_id': chat_id}
    if message_thread_id is not None:
        data['message_thread_id'] = message_thread_id
    if caption:
        data['caption'] = caption[:1024]
    response = _request(
        f'Telegram {method}', 'POST', f'https://api.telegram.org/bot{TG_TOKEN}/{method}',
        data=data, files={field_name: (filename, file_data, content_type)}, timeout=(10, 30)
    )
    _raise_for_api_error(response, f'Telegram {method}')

def send_telegram_message(chat_id, text, reply_markup=None, message_thread_id=None):
    payload = {'chat_id': chat_id, 'text': text}
    if reply_markup is not None:
        payload['reply_markup'] = reply_markup
    if message_thread_id is not None:
        payload['message_thread_id'] = message_thread_id
    response = _request(
        'Telegram sendMessage', 'POST', f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
        json=payload, timeout=(5, 15)
    )
    _raise_for_api_error(response, 'Telegram sendMessage')

def send_telegram_callback_answer(callback_id, text='', show_alert=False):
    response = _request(
        'Telegram answerCallbackQuery', 'POST', f'https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery',
        json={
            'callback_query_id': callback_id,
            'text':              text,
            'show_alert':        show_alert
            }, timeout=(5, 15)
    )
    _raise_for_api_error(response, 'Telegram answerCallbackQuery')

def _command(text):
    if not isinstance(text, str) or not text.strip():
        return ''
    return text.strip().split(maxsplit=1)[0].split('@', 1)[0].lower()

def _is_video_file(filename, content_type):
    return (
        isinstance(content_type, str) and content_type.lower().startswith('video/')
    ) or (
        isinstance(filename, str)
        and os.path.splitext(filename)[1].lower() in VIDEO_EXTENSIONS
    )

def _telegram_forward_source(message):
    origin = message.get('forward_origin')
    source_user = None
    source_chat = None
    source_message_id = None
    if isinstance(origin, dict):
        if origin.get('type') == 'user':
            source_user = origin.get('sender_user')
        elif origin.get('type') == 'channel':
            source_chat = origin.get('chat')
            source_message_id = origin.get('message_id')
        elif origin.get('type') == 'chat':
            source_chat = origin.get('sender_chat')
        elif origin.get('type') == 'hidden_user':
            hidden_name = origin.get('sender_user_name')
            if isinstance(hidden_name, str) and hidden_name.strip():
                return f'Сообщение от {hidden_name.strip()}'
    if not isinstance(source_user, dict):
        source_user = message.get('forward_from')
    if isinstance(source_user, dict):
        first_name = source_user.get('first_name')
        last_name = source_user.get('last_name')
        title = ' '.join(
            part.strip() for part in (first_name, last_name)
            if isinstance(part, str) and part.strip()
        )
        username = source_user.get('username')
        user_id = source_user.get('id')
        if not title:
            title = f'@{username}' if isinstance(username, str) and username else ''
        if isinstance(username, str) and re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
            source_url = f'https://t.me/{username}'
        elif isinstance(user_id, int) and user_id > 0:
            source_url = f'tg://user?id={user_id}'
        else:
            source_url = ''
        if title and source_url:
            return f'Сообщение от {title} ({source_url})'
    if not isinstance(source_chat, dict):
        source_chat = message.get('forward_from_chat')
        source_message_id = message.get('forward_from_message_id')
    if not isinstance(source_chat, dict):
        source_chat = message.get('sender_chat')
        source_message_id = message.get('message_id')
    if not isinstance(source_chat, dict):
        return None

    title = source_chat.get('title')
    username = source_chat.get('username')
    if not isinstance(title, str) or not title.strip():
        title = f'@{username}' if isinstance(username, str) else ''
    if not title:
        return None

    if isinstance(username, str) and re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
        source_url = f'https://t.me/{username}'
    else:
        chat_id = source_chat.get('id')
        chat_id_text = str(chat_id) if isinstance(chat_id, int) else ''
        if not chat_id_text.startswith('-100') or not isinstance(source_message_id, int):
            return None
        source_url = f'https://t.me/c/{chat_id_text[4:]}/{source_message_id}'
    return f'Источник: {title.strip()} ({source_url})'

def _owner_id():
    try:
        return int(TG_OWNER_ID)
    except (TypeError, ValueError):
        return None

def _forum_chat_id():
    if not TG_FORUM_CHAT_ID:
        return None
    try:
        chat_id = int(TG_FORUM_CHAT_ID)
    except (TypeError, ValueError):
        return None
    return chat_id if chat_id < 0 else None

def create_telegram_forum_topic(name):
    response = _request(
        'Telegram createForumTopic', 'POST',
        f'https://api.telegram.org/bot{TG_TOKEN}/createForumTopic',
        json={'chat_id': _forum_chat_id(), 'name': name[:128]}, timeout=(5, 15)
    )
    payload = _raise_for_api_error(response, 'Telegram createForumTopic')
    message_thread_id = safe_dict_get(payload, 'result', 'message_thread_id')
    if not isinstance(message_thread_id, int) or message_thread_id <= 0:
        raise RuntimeError('Telegram createForumTopic returned an invalid topic id')
    logger.info('Created Telegram forum topic: message_thread_id=%s', message_thread_id)
    return message_thread_id
