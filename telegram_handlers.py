import mimetypes

import storage
from bridge_common import (
    MAX_MEDIA_BYTES, MAX_MESSAGE_LENGTH, MAX_PHOTO_BYTES, VIDEO_UNAVAILABLE_NOTICE,
    _command, _download_telegram_file, _forum_chat_id, _is_video_file, _owner_id,
    _safe_filename, _telegram_forward_source, _upload_vk_document, _upload_vk_message_photo,
    logger, safe_dict_get, send_telegram_callback_answer, send_telegram_message, send_vk_message,
)

__all__ = [
    '_event_chat_context', '_handle_telegram_album', '_handle_telegram_callback',
    '_handle_telegram_message', '_handle_telegram_update',
]


def _handle_telegram_callback(callback):
    callback_id = callback.get('id')
    actor_id = safe_dict_get(callback, 'from', 'id')
    chat_id = safe_dict_get(callback, 'message', 'chat', 'id')
    if actor_id != _owner_id() or chat_id != _forum_chat_id():
        logger.info(
            'Ignoring Telegram callback: actor_is_owner=%s, chat_is_forum=%s',
            actor_id == _owner_id(), chat_id == _forum_chat_id()
        )
        return
    if callback_id:
        send_telegram_callback_answer(callback_id, 'Используйте отдельные темы форума для переписки.')

def _handle_telegram_message(message, event_id):
    chat = message.get('chat')
    sender = message.get('from')
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        logger.warning('Ignoring Telegram update %s without a valid message chat or sender', event_id)
        return
    owner_id = _owner_id()
    is_forum_chat = (
        chat.get('id') == _forum_chat_id()
        and chat.get('type') == 'supergroup'
    )
    sender_is_owner = sender.get('id') == owner_id
    if not is_forum_chat or not sender_is_owner:
        logger.info(
            'Ignoring Telegram message %s: forum_chat=%s, sender_is_owner=%s',
            event_id, is_forum_chat, sender_is_owner
        )
        return

    telegram_chat_id = chat.get('id')
    message_thread_id = message.get('message_thread_id') if is_forum_chat else None

    def reply(reply_text):
        send_telegram_message(telegram_chat_id, reply_text, message_thread_id=message_thread_id)

    text = message.get('text') or message.get('caption') or ''
    if not isinstance(text, str):
        text = ''
    command = _command(message.get('text'))
    logger.info(
        'Processing Telegram message %s: command=%s, has_text=%s, has_photo=%s, has_video=%s, has_document=%s',
        event_id, command or 'none', bool(text), 'photo' in message,
        'video' in message or 'video_note' in message, 'document' in message
    )
    if command in {'/start', '/help'}:
        reply('Пишите ответ в теме нужного VK-собеседника. Новая тема создаётся после его /connect.')
        return
    if command == '/list':
        reply('Для каждого VK-собеседника используется отдельная тема. Пишите в нужной теме.')
        return
    if command == '/stop':
        reply('Для завершения переписки просто прекратите писать в эту тему. Чтобы отозвать согласие VK, отправьте /disconnect.')
        return

    document = message.get('document')
    document_name = document.get('file_name', '') if isinstance(document, dict) else ''
    document_mime = document.get('mime_type', '') if isinstance(document, dict) else ''
    is_gif_document = (
        isinstance(document_name, str) and document_name.lower().endswith('.gif')
    ) or document_mime == 'image/gif'
    is_video_document = _is_video_file(document_name, document_mime)
    has_video = any(
        isinstance(message.get(field), dict)
        for field in ('video_note', 'video', 'animation')
    ) or is_video_document
    unsupported_fields = {'audio', 'voice', 'sticker', 'contact', 'location', 'venue', 'poll', 'dice'}
    if unsupported_fields.intersection(message):
        reply('Отправка видео пока недоступна. Разработка продолжается, наверное...')
        return

    if not isinstance(message_thread_id, int):
        reply('Отправляйте сообщения внутри темы VK-собеседника.')
        return
    selected = storage.get_forum_contact(message_thread_id)
    if selected is None:
        reply('Эта тема не связана с активным VK-собеседником. Попросите его отправить /connect.')
        return
    if len(text) > MAX_MESSAGE_LENGTH:
        reply('Сообщение слишком длинное. Максимум 3500 символов.')
        return
    forward_source = _telegram_forward_source(message)
    if forward_source:
        text = f'{text}\n\n{forward_source}' if text else forward_source
    attachments = []
    photo_groups = message.get('media_group_photos')
    if photo_groups is None:
        photo = message.get('photo')
        photo_groups = [photo] if photo is not None else []
    for photos in photo_groups:
        if not isinstance(photos, list) or not photos or not isinstance(photos[-1], dict):
            reply('Не удалось обработать фотографию.')
            return
        file_id = photos[-1].get('file_id')
        if not isinstance(file_id, str) or not file_id:
            reply('Не удалось обработать фотографию.')
            return
        try:
            image_data, image_content_type = _download_telegram_file(file_id, MAX_PHOTO_BYTES)
        except ValueError:
            logger.warning('Rejected oversized Telegram photo: event_id=%s max_bytes=%s', event_id, MAX_PHOTO_BYTES)
            reply('Фотография превышает лимит 10 МБ.')
            return
        if not isinstance(image_content_type, str) or not image_content_type.startswith('image/'):
            image_content_type = 'image/jpeg'
        attachments.append(_upload_vk_message_photo(selected['vk_user_id'], image_data, image_content_type))

    media_message = None
    media_kind = None
    if is_gif_document and isinstance(document, dict):
        media_message = document
        media_kind = 'gif'
    elif isinstance(document, dict) and not is_video_document:
        media_message = document
        media_kind = 'document'

    if media_message is not None:
        file_id = media_message.get('file_id')
        if not isinstance(file_id, str) or not file_id:
            reply('Не удалось обработать файл.')
            return
        try:
            file_data, downloaded_content_type = _download_telegram_file(file_id, MAX_MEDIA_BYTES)
        except ValueError:
            logger.warning(
                'Rejected oversized Telegram media: event_id=%s max_bytes=%s',
                event_id, MAX_MEDIA_BYTES
            )
            reply('Файл превышает лимит 20 МБ.')
            return
        filename = media_message.get('file_name')
        if not isinstance(filename, str) or not filename.strip():
            filename = {
                'video':     'video.mp4',
                'animation': 'animation.mp4',
                'gif':       'animation.gif',
                'document':  'document'
            }[media_kind]
        filename = _safe_filename(filename, 'document')
        content_type = media_message.get('mime_type')
        if not isinstance(content_type, str) or '/' not in content_type:
            content_type = downloaded_content_type or mimetypes.guess_type(filename)[0] or 'application/octet-stream'

        attachments.append(
            _upload_vk_document(selected['vk_user_id'], file_data, filename, content_type)
        )

    if not text and not attachments:
        if not has_video:
            reply('Поддерживаются текст, фотографии, GIF и документы; видео не пересылаются.')
            return
    current_selection = storage.get_forum_contact(message_thread_id)
    if current_selection is None or current_selection['vk_user_id'] != selected['vk_user_id']:
        reply('Согласие или привязка темы изменились. Проверьте подключение VK-собеседника.')
        return
    if text or attachments:
        send_vk_message(selected['vk_user_id'], text, ','.join(attachments), event_id=f'tg:{event_id}')
    if has_video:
        reply(VIDEO_UNAVAILABLE_NOTICE)
    logger.info(
        'Forwarded Telegram message: event_id=%s topic=%s vk_user_id=%s has_attachment=%s',
        event_id, message_thread_id, selected['vk_user_id'], bool(attachments)
    )

def _handle_telegram_update(update, event_id):
    callback = update.get('callback_query')
    if isinstance(callback, dict):
        _handle_telegram_callback(callback)
        return
    message = update.get('message')
    if isinstance(message, dict):
        _handle_telegram_message(message, event_id)
        return
    logger.info(
        'Ignoring unsupported Telegram update %s: keys=%s', event_id,
        ','.join(sorted(key for key in update if isinstance(key, str)))
    )

def _handle_telegram_album(messages, event_ids):
    if len(messages) < 2 or not all(isinstance(message.get('photo'), list) for message in messages):
        for message, event_id in zip(messages, event_ids):
            _handle_telegram_message(message, event_id)
        return

    combined = dict(messages[0])
    combined['media_group_photos'] = [message['photo'] for message in messages]
    captions = []
    for message in messages:
        caption = message.get('text') or message.get('caption')
        if isinstance(caption, str) and caption.strip() and caption not in captions:
            captions.append(caption.strip())
        for key in ('forward_origin', 'forward_from_chat', 'forward_from_message_id', 'sender_chat'):
            if not combined.get(key) and message.get(key):
                combined[key] = message[key]
    combined['text'] = '\n'.join(captions)
    combined.pop('caption', None)
    _handle_telegram_message(combined, event_ids[0])

def _event_chat_context(provider, payload):
    if provider == 'telegram':
        message = safe_dict_get(payload, 'message')
        if not isinstance(message, dict):
            message = safe_dict_get(payload, 'callback_query', 'message')
        thread_id = safe_dict_get(message, 'message_thread_id')
        if not isinstance(thread_id, int):
            return None
        try:
            contact = storage.get_forum_contact(thread_id)
        except Exception as error:
            logger.warning(
                'Could not resolve Telegram topic contact for error report: thread_id=%s error=%s',
                thread_id, type(error).__name__
            )
            return None
        if contact and isinstance(contact.get('display_name'), str) and contact['display_name'].strip():
            return contact['display_name'].strip()
    elif provider == 'vk':
        user_id = safe_dict_get(payload, 'from_id')
        if user_id is not None:
            try:
                contact = storage.get_forum_contact_by_vk_user(user_id)
            except Exception as error:
                logger.warning(
                    'Could not resolve VK contact for error report: user_id=%s error=%s',
                    user_id, type(error).__name__
                )
                return None
            if contact and isinstance(contact.get('display_name'), str) and contact['display_name'].strip():
                return contact['display_name'].strip()
    return None
