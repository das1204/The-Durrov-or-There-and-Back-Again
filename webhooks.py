import hmac
import os
import threading
import time

from flask import Flask, jsonify, request
from werkzeug.exceptions import (
    BadRequest,
    HTTPException,
    RequestEntityTooLarge,
    UnsupportedMediaType,
)

import storage
from bridge_common import (
    TG_WEBHOOK_SECRET, VK_CALLBACK_SECRET, VK_CONFIRMATION, _ensure_errors_topic,
    _error_context, _log_exception, ensure_database, logger,
    require_bridge_config, safe_dict_get,
)
from telegram_handlers import (
    _event_chat_context, _handle_telegram_album, _handle_telegram_update,
)
from vk_handlers import _handle_vk_message

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024
_queue_workers_pid = None
_queue_workers_lock = threading.Lock()

@app.errorhandler(RequestEntityTooLarge)
def request_too_large(error):
    logger.warning(
        'Rejected oversized request: path=%s content_length=%s error=%s',
        request.path, request.content_length, error.name
    )
    return jsonify({'ok': False, 'error': 'request_too_large'}), 413

@app.errorhandler(Exception)
def handle_unexpected_error(error):
    if isinstance(error, HTTPException):
        logger.warning(
            'HTTP request failed: method=%s path=%s status=%s error=%s',
            request.method, request.path, error.code, error.name
        )
        return error
    _log_exception(
        'Unhandled Flask error: path=%s method=%s',
        request.path,
        request.method,
        exception=error
    )
    return jsonify({'ok': False, 'error': 'internal_server_error'}), 500

def _queue_worker(provider):
    while True:
        try:
            events = storage.claim_next_webhook_events(provider)
        except Exception:
            _error_context.chat = None
            try:
                pending_event = storage.peek_next_webhook_event(provider)
                if pending_event and isinstance(pending_event.get('payload'), dict):
                    _error_context.chat = _event_chat_context(provider, pending_event['payload'])
            except Exception as context_error:
                logger.warning(
                    'Could not resolve chat name for webhook queue error: provider=%s error=%s',
                    provider, type(context_error).__name__
                )
            _log_exception('Failed to claim webhook queue item: provider=%s', provider)
            _error_context.chat = None
            time.sleep(2)
            continue
        if not events:
            time.sleep(0.5)
            continue

        queue_ids = [event['queue_id'] for event in events]
        event_ids = ','.join(event['event_id'] for event in events)
        _error_context.chat = _event_chat_context(provider, events[0]['payload'])
        try:
            if provider == 'telegram':
                messages = [safe_dict_get(event['payload'], 'message') for event in events]
                if len(events) > 1 and all(isinstance(message, dict) for message in messages):
                    _handle_telegram_album(messages, [event['event_id'] for event in events])
                else:
                    _handle_telegram_update(events[0]['payload'], events[0]['event_id'])
            else:
                _handle_vk_message(events[0]['payload'], events[0]['event_id'])
            storage.finish_webhook_events(queue_ids, succeeded=True)
            logger.info('Finished queued webhook events: provider=%s event_ids=%s', provider, event_ids)
        except Exception:
            _log_exception('Queued webhook processing failed: provider=%s event_ids=%s', provider, event_ids)
            try:
                storage.finish_webhook_events(queue_ids, succeeded=False)
            except Exception:
                _log_exception(
                    'Failed to update queued webhook events: provider=%s event_ids=%s', provider, event_ids
                )
        finally:
            _error_context.chat = None
def _start_queue_workers():
    global _queue_workers_pid
    process_id = os.getpid()
    with _queue_workers_lock:
        if _queue_workers_pid == process_id:
            return
        for provider in ('telegram', 'vk'):
            threading.Thread(
                target=_queue_worker, args=(provider,), name=f'{provider}-webhook-queue', daemon=True
            ).start()
        _queue_workers_pid = process_id


def _verify_telegram_webhook():
    provided = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    return bool(TG_WEBHOOK_SECRET) and hmac.compare_digest(provided, TG_WEBHOOK_SECRET)

@app.route('/tg_webhook', methods=['POST'])
def tg_webhook():
    if not _verify_telegram_webhook():
        logger.warning('Rejected Telegram webhook request: secret token mismatch')
        return jsonify({'ok': False, 'error': 'unauthorized'}), 403
    try:
        require_bridge_config()
    except RuntimeError as exc:
        logger.warning(str(exc))
        return jsonify({'ok': False, 'error': 'bridge_not_configured'}), 503

    try:
        update = request.get_json()
    except RequestEntityTooLarge as exc:
        return request_too_large(exc)
    except BadRequest:
        logger.warning('Rejected Telegram webhook request: malformed JSON, content_type=%s', request.mimetype)
        return jsonify({'ok': False, 'error': 'invalid_payload'}), 400
    except UnsupportedMediaType:
        logger.warning('Rejected Telegram webhook request: unsupported content_type=%s', request.mimetype)
        return jsonify({'ok': False, 'error': 'invalid_content_type'}), 415
    if not isinstance(update, dict) or not isinstance(update.get('update_id'), int):
        logger.warning('Rejected Telegram webhook request: invalid update payload')
        return jsonify({'ok': False, 'error': 'invalid_payload'}), 400
    event_id = str(update['update_id'])
    logger.info(
        'Received Telegram webhook update %s: keys=%s', event_id,
        ','.join(sorted(key for key in update if isinstance(key, str)))
    )
    try:
        ensure_database()
        _ensure_errors_topic()
        _start_queue_workers()
        queued = storage.enqueue_webhook_event('telegram', event_id, update)
    except Exception:
        _error_context.chat = _event_chat_context('telegram', update)
        _log_exception('Failed to enqueue Telegram webhook event_id=%s', event_id)
        _error_context.chat = None
        return jsonify({'ok': False, 'error': 'telegram_queue_unavailable'}), 503
    if not queued:
        logger.info('Ignoring duplicate Telegram webhook event_id=%s', event_id)
        return jsonify({'ok': True})
    logger.info('Queued Telegram webhook event_id=%s', event_id)
    return jsonify({'ok': True, 'queued': True})

@app.route('/vk_callback', methods=['POST'])
def vk_callback():
    try:
        require_bridge_config()
    except RuntimeError as exc:
        logger.warning(str(exc))
        return jsonify({'ok': False, 'error': 'bridge_not_configured'}), 503

    try:
        data = request.get_json()
    except RequestEntityTooLarge as exc:
        return request_too_large(exc)
    except BadRequest:
        logger.warning('Rejected VK callback request: malformed JSON, content_type=%s', request.mimetype)
        return jsonify({'ok': False, 'error': 'invalid_payload'}), 400
    except UnsupportedMediaType:
        logger.warning('Rejected VK callback request: unsupported content_type=%s', request.mimetype)
        return jsonify({'ok': False, 'error': 'invalid_content_type'}), 415
    if not isinstance(data, dict):
        logger.warning('Rejected VK callback request: expected JSON object, received=%s', type(data).__name__)
        return jsonify({'ok': False, 'error': 'invalid_payload'}), 400
    provided_secret = data.get('secret', '')
    if not isinstance(provided_secret, str) or not hmac.compare_digest(provided_secret, VK_CALLBACK_SECRET):
        logger.warning('Rejected VK callback request: secret mismatch')
        return jsonify({'ok': False, 'error': 'unauthorized'}), 403

    if data.get('type') == 'confirmation':
        logger.info('VK Callback API confirmation request received')
        return VK_CONFIRMATION

    if data.get('type') != 'message_new':
        logger.debug('Ignoring unsupported VK callback type=%s', data.get('type'))
        return 'ok'
    event_id = data.get('event_id')
    if not isinstance(event_id, str) or not event_id:
        logger.warning('Rejected VK message_new callback: missing event_id')
        return jsonify({'ok': False, 'error': 'invalid_payload'}), 400
    message = safe_dict_get(data, 'object', 'message')
    if not isinstance(message, dict):
        logger.warning('Rejected VK message_new callback: invalid message payload, event_id=%s', event_id)
        return jsonify({'ok': False, 'error': 'invalid_payload'}), 400

    try:
        ensure_database()
        _ensure_errors_topic()
        _start_queue_workers()
        queued = storage.enqueue_webhook_event('vk', event_id, message)
    except Exception:
        _error_context.chat = _event_chat_context('vk', message)
        _log_exception('Failed to enqueue VK webhook event_id=%s', event_id)
        _error_context.chat = None
        return jsonify({'ok': False, 'error': 'vk_queue_unavailable'}), 503
    if not queued:
        logger.info('Ignoring duplicate VK webhook event_id=%s', event_id)
        return 'ok'
    logger.info('Queued VK webhook event_id=%s', event_id)
    return 'ok'

@app.route('/healthz')
def healthz():
    try:
        require_bridge_config()
        configured = True
    except RuntimeError as exc:
        logger.warning('Health check found invalid configuration: %s', exc)
        configured = False
    except Exception:
        _log_exception('Health check failed unexpectedly')
        configured = False
    status_code = 200 if configured else 503
    return jsonify({
        'status':     'ok' if configured else 'misconfigured',
        'configured': configured
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
        _log_exception('Readiness check failed')
        database_ready = False
    status_code = 200 if database_ready else 503
    return jsonify({
        'status':         'ready' if database_ready else 'not_ready',
        'database_ready': database_ready
    }), status_code

@app.route('/')
def index():
    return '✅ Бот-мост работает!'
