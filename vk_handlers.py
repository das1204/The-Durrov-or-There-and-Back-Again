import json
import mimetypes
import re
from urllib.parse import urlparse

import storage
from bridge_common import (
    MAX_MEDIA_BYTES, MAX_MESSAGE_LENGTH, MAX_PHOTO_BYTES, TG_TOKEN, VK_TOKEN,
    VIDEO_UNAVAILABLE_NOTICE,
    _command, _download_vk_document, _download_vk_file, _forum_chat_id,
    _is_video_file, _is_vk_document_url, _log_exception, _raise_for_api_error,
    _request, _safe_filename, _send_telegram_file, create_telegram_forum_topic,
    logger, safe_dict_get, send_telegram_message, send_vk_message,
)

__all__ = [
    '_handle_vk_message', '_vk_display_name', '_vk_forwarded_messages',
    '_vk_group_source', '_vk_send_id',
]


def _vk_display_name(vk_user_id):
    try:
        response = _request(
            'VK users.get', 'GET', 'https://api.vk.com/method/users.get',
            params={'user_ids': vk_user_id, 'access_token': VK_TOKEN, 'v': '5.199'}, timeout=(5, 15)
        )
        data = _raise_for_api_error(response, 'VK users.get')
        users = safe_dict_get(data, 'response')
        if isinstance(users, list) and users and isinstance(users[0], dict):
            name = ' '.join(part for part in (users[0].get('first_name'), users[0].get('last_name')) if isinstance(part, str)).strip()
            if name:
                return name[:80]
    except Exception:
        _log_exception('Failed to resolve display name for VK user %s; using fallback', vk_user_id)
    return 'Пользователь VK'

def _vk_group_source(group_id):
    name = 'Сообщество VK'
    source_url = f'https://vk.com/club{group_id}'
    try:
        response = _request(
            'VK groups.getById', 'GET', 'https://api.vk.com/method/groups.getById',
            params={'group_ids': group_id, 'access_token': VK_TOKEN, 'v': '5.199'}, timeout=(5, 15)
        )
        data = _raise_for_api_error(response, 'VK groups.getById')
        groups = safe_dict_get(data, 'response')
        if isinstance(groups, dict):
            groups = groups.get('groups', [groups])
        if isinstance(groups, list) and groups and isinstance(groups[0], dict):
            group = groups[0]
            group_name = group.get('name')
            if isinstance(group_name, str) and group_name.strip():
                name = ' '.join(group_name.split())[:80]
            screen_name = group.get('screen_name')
            if isinstance(screen_name, str) and re.fullmatch(r'[A-Za-z0-9_.]+', screen_name):
                source_url = f'https://vk.com/{screen_name}'
    except Exception:
        _log_exception('Failed to resolve VK community source group_id=%s; using fallback', group_id)
    return name, source_url

def _vk_forwarded_messages(message):
    forwarded_messages = message.get('fwd_messages')
    if not isinstance(forwarded_messages, list):
        return []

    collected = []
    pending = list(forwarded_messages)
    while pending:
        forwarded_message = pending.pop(0)
        if not isinstance(forwarded_message, dict):
            continue
        collected.append(forwarded_message)
        nested_messages = forwarded_message.get('fwd_messages')
        if isinstance(nested_messages, list):
            pending[0:0] = nested_messages
    return collected

def _vk_send_id(event_id, suffix='reply'):
    return f'vk:{event_id}:{suffix}'

def _handle_vk_message(message, event_id):
    vk_user_id = message.get('from_id')
    if not isinstance(vk_user_id, int) or vk_user_id <= 0:
        logger.warning(
            'Ignoring VK message with invalid from_id: event_id=%s from_id_type=%s', event_id, type(vk_user_id).__name__
        )
        return
    text = message.get('text', '')
    if not isinstance(text, str):
        text = ''
    command = _command(text)
    logger.info('Processing VK message event_id=%s, vk_user_id=%s, command=%s', event_id, vk_user_id, command or 'none')

    if command == '/connect':
        display_name = _vk_display_name(vk_user_id)
        storage.register_consent(vk_user_id, display_name)
        topic = storage.get_forum_topic(vk_user_id)
        if topic is None:
            message_thread_id = create_telegram_forum_topic(display_name)
            storage.save_forum_topic(vk_user_id, message_thread_id)
            logger.info(
                'Linked VK user to Telegram topic: vk_user_id=%s topic=%s', vk_user_id, message_thread_id
            )
            send_telegram_message(
                _forum_chat_id(), f'Тема для {display_name}. Ответы из этой темы будут отправляться этому VK-пользователю.',
                message_thread_id=message_thread_id
            )
        send_vk_message(
            vk_user_id, 'Согласие сохранено. Для переписки используйте отдельную тему в Telegram-форуме. Для отзыва согласия отправьте /disconnect.',
            event_id=_vk_send_id(event_id, 'connect')
        )
        return
    if command == '/disconnect':
        storage.revoke_consent(vk_user_id)
        send_vk_message(
            vk_user_id, 'Согласие отозвано. Новые сообщения пересылаться не будут.',
            event_id=_vk_send_id(event_id, 'disconnect')
        )
        return
    if command in {'/start', '/help'}:
        send_vk_message(
            vk_user_id, 'Чтобы разрешить сообщения, отправьте /connect. Для прекращения и отзыва согласия отправьте /disconnect.',
            event_id=_vk_send_id(event_id, 'help')
        )
        return

    selected = storage.get_forum_contact_by_vk_user(vk_user_id)
    if selected is None:
        logger.debug('Ignoring VK message event_id=%s from unconnected user_id=%s', event_id, vk_user_id)
        return
    telegram_chat_id = _forum_chat_id()
    message_thread_id = selected['message_thread_id']

    def notify_owner(notification):
        send_telegram_message(telegram_chat_id, notification, message_thread_id=message_thread_id)

    photo_urls = []
    media_files = []
    has_video = False
    wall_sources = {}
    forwarded_messages = _vk_forwarded_messages(message)
    unsupported_attachment = False
    attachments = message.get('attachments', [])
    if not isinstance(attachments, list):
        attachments = []
    else:
        attachments = list(attachments)
    forwarded_texts = []
    for forwarded_message in forwarded_messages:
        forwarded_text = forwarded_message.get('text')
        if isinstance(forwarded_text, str) and forwarded_text.strip():
            forwarded_texts.append(forwarded_text.strip())
        forwarded_attachments = forwarded_message.get('attachments')
        if isinstance(forwarded_attachments, list):
            attachments.extend(forwarded_attachments)
    if forwarded_texts:
        forwarded_body = '\n\n'.join(forwarded_texts)
        text = f'{text}\n\n{forwarded_body}' if text else forwarded_body
    seen_forwarded_user_ids = set()
    for forwarded_message in forwarded_messages:
        user_id = forwarded_message.get('from_id')
        if not isinstance(user_id, int) or user_id <= 0 or user_id in seen_forwarded_user_ids:
            continue
        seen_forwarded_user_ids.add(user_id)
        display_name = _vk_display_name(user_id)
        forwarded_attribution = f'Сообщение от {display_name} (https://vk.com/id{user_id})'
        text = f'{text}\n\n{forwarded_attribution}' if text else forwarded_attribution
    wall_texts = []
    if isinstance(attachments, list):
        attachments_to_process = list(attachments)
        attachment_index = 0
        while attachment_index < len(attachments_to_process):
            item = attachments_to_process[attachment_index]
            attachment_index += 1
            if not isinstance(item, dict):
                continue
            attachment_type = item.get('type')
            if attachment_type == 'wall':
                wall = item.get('wall')
                if not isinstance(wall, dict):
                    unsupported_attachment = True
                    continue
                wall_text = wall.get('text')
                if isinstance(wall_text, str) and wall_text.strip():
                    wall_texts.append(wall_text.strip())
                wall_owner_id = wall.get('owner_id', wall.get('from_id'))
                if isinstance(wall_owner_id, int) and wall_owner_id < 0:
                    group_id = abs(wall_owner_id)
                    if group_id not in wall_sources:
                        wall_sources[group_id] = _vk_group_source(group_id)
                wall_attachments = wall.get('attachments')
                if isinstance(wall_attachments, list):
                    attachments_to_process.extend(wall_attachments)
                copy_history = wall.get('copy_history')
                if isinstance(copy_history, list):
                    attachments_to_process.extend(
                        {'type': 'wall', 'wall': copied_post} for copied_post in copy_history if isinstance(copied_post, dict)
                    )
                continue
            if attachment_type == 'photo':
                sizes = safe_dict_get(item, 'photo', 'sizes')
                if not isinstance(sizes, list):
                    unsupported_attachment = True
                    continue
                valid_sizes = [size for size in sizes if isinstance(size, dict) and isinstance(size.get('url'), str)]
                if valid_sizes:
                    photo_urls.append(
                        max(valid_sizes, key=lambda size: (size.get('width', 0) or 0) * (size.get('height', 0) or 0))['url']
                    )
                else:
                    unsupported_attachment = True
                continue
            if attachment_type in {'video', 'video_message'}:
                has_video = True
                continue
            if attachment_type == 'doc':
                document = item.get('doc')
                document_url = document.get('url') if isinstance(document, dict) else None
                if not _is_vk_document_url(document_url):
                    try:
                        document_host = urlparse(document_url).hostname
                    except (TypeError, ValueError):
                        document_host = None
                    logger.warning(
                        'Skipping VK document with invalid URL: event_id=%s host=%s',
                        event_id, document_host or 'missing'
                    )
                    unsupported_attachment = True
                    continue
                extension = document.get('ext', '') if isinstance(document, dict) else ''
                title = document.get('title', '') if isinstance(document, dict) else ''
                document_mime = document.get('mime_type', '') if isinstance(document, dict) else ''
                if isinstance(extension, str):
                    extension = ''.join(character for character in extension if character.isalnum())[:16]
                else:
                    extension = ''
                filename = _safe_filename(title, f'document.{extension}' if extension else 'document')
                if extension and not filename.lower().endswith(f'.{extension.lower()}'):
                    filename = f'{filename}.{extension}'
                if _is_video_file(filename, document_mime):
                    has_video = True
                    continue
                if extension.lower() == 'gif':
                    media_kind = 'animation'
                else:
                    media_kind = 'document'
                media_files.append((media_kind, document_url, filename))
                continue
            logger.info('Skipping unsupported VK attachment type=%s event_id=%s', attachment_type, event_id)
            unsupported_attachment = True

    if wall_texts:
        wall_text = '\n\n'.join(wall_texts)
        text = f'{text}\n\n{wall_text}' if text else wall_text
    source_attribution = ''
    if wall_sources:
        sources = '; '.join(f'{name} ({source_url})' for name, source_url in wall_sources.values())
        source_attribution = f'Источник: {sources}'
        text = f'{text}\n\n{source_attribution}' if text else source_attribution
    if unsupported_attachment:
        notify_owner('Часть вложений VK пропущена: поддерживаются фотографии, GIF и документы.')
    if not text and not photo_urls and not media_files and not has_video:
        return
    current_contact = storage.get_forum_contact_by_vk_user(vk_user_id)
    if (current_contact is None or current_contact['message_thread_id'] != message_thread_id):
        return

    if len(text) > MAX_MESSAGE_LENGTH:
        if source_attribution:
            text_before_attribution = text[:-len(source_attribution)].rstrip()
            separator = '\n\n' if text_before_attribution else ''
            max_content_length = MAX_MESSAGE_LENGTH - len(source_attribution) - len(separator)
            text = f'{text_before_attribution[:max(0, max_content_length)]}{separator}{source_attribution}'
        else:
            text = text[:MAX_MESSAGE_LENGTH]
    caption_sent = False
    if photo_urls:
        for start in range(0, len(photo_urls), 10):
            photo_batch = photo_urls[start:start + 10]
            caption = text[:1024] if text and not caption_sent else None
            downloaded_photos = []
            for photo_url in photo_batch:
                try:
                    image_data, content_type = _download_vk_file(photo_url, MAX_PHOTO_BYTES, allow_photo_cdn=True)
                except ValueError:
                    logger.warning('Rejected oversized VK photo: event_id=%s max_bytes=%s', event_id, MAX_PHOTO_BYTES)
                    notify_owner('Фотография из VK превышает лимит 10 МБ; она пропущена.')
                    continue
                downloaded_photos.append((image_data, content_type or 'image/jpeg'))
            if not downloaded_photos:
                continue
            if len(downloaded_photos) == 1:
                response = _request(
                    'Telegram sendPhoto', 'POST', f'https://api.telegram.org/bot{TG_TOKEN}/sendPhoto',
                    data={
                        'chat_id': telegram_chat_id,
                        **({'caption': caption} if caption else {}),
                        'message_thread_id': message_thread_id
                    },
                    files={'photo': ('photo.jpg', downloaded_photos[0][0], downloaded_photos[0][1])},
                    timeout=(10, 30)
                )
                _raise_for_api_error(response, 'Telegram sendPhoto')
            else:
                media = [{'type': 'photo', 'media': f'attach://photo{index}'} for index in range(len(downloaded_photos))]
                if caption:
                    media[0]['caption'] = caption
                response = _request(
                    'Telegram sendMediaGroup', 'POST', f'https://api.telegram.org/bot{TG_TOKEN}/sendMediaGroup',
                    data={
                        'chat_id':           telegram_chat_id,
                        'media':             json.dumps(media),
                        'message_thread_id': message_thread_id
                    },
                    files={
                        f'photo{index}': (f'photo{index}.jpg', image_data, content_type)
                        for index, (image_data, content_type) in enumerate(downloaded_photos)
                    }, timeout=(10, 30)
                )
                _raise_for_api_error(response, 'Telegram sendMediaGroup')
            caption_sent = caption_sent or bool(caption)

    for media_kind, document_url, filename in media_files:
        try:
            file_data, content_type = _download_vk_document(document_url, MAX_MEDIA_BYTES)
        except ValueError:
            logger.warning(
                'Rejected oversized VK media: event_id=%s max_bytes=%s filename=%s', event_id, MAX_MEDIA_BYTES, filename
            )
            notify_owner(f'Вложение «{filename}» превышает лимит 20 МБ; оно пропущено.')
            continue
        except RuntimeError:
            _log_exception('Failed to download VK attachment: event_id=%s filename=%s', event_id, filename)
            notify_owner(f'Не удалось скачать вложение «{filename}» из VK; оно пропущено.')
            continue
        content_type = content_type or mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        caption = text[:1024] if text and not caption_sent else ''
        if media_kind == 'animation':
            telegram_method, field_name = 'sendAnimation', 'animation'
        else:
            telegram_method, field_name = 'sendDocument', 'document'
        _send_telegram_file(
            telegram_chat_id, telegram_method, field_name,
            file_data, filename, content_type, caption, message_thread_id
        )
        logger.info(
            'Sent VK attachment to Telegram: event_id=%s media_kind=%s method=%s filename=%s topic=%s',
            event_id, media_kind, telegram_method, filename, message_thread_id
        )
        caption_sent = caption_sent or bool(caption)
    if text and not caption_sent:
        notify_owner(text)
    elif len(text) > 1024:
        notify_owner(text[1024:])
    if has_video:
        send_vk_message(
            vk_user_id,
            VIDEO_UNAVAILABLE_NOTICE,
            event_id=_vk_send_id(event_id, 'video-notice')
        )
    logger.info('Forwarded VK message event_id=%s to Telegram topic=%s', event_id, message_thread_id)
