import os
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

TELEGRAM_MEDIA_GROUP_DEBOUNCE_SECONDS = 3


SCHEMA_STATEMENTS = (
"""
CREATE TABLE IF NOT EXISTS vk_contacts (
    vk_user_id BIGINT PRIMARY KEY,
    display_name TEXT NOT NULL,
    consented_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ
);
""",
"""
CREATE TABLE IF NOT EXISTS telegram_selection (
    owner_id BIGINT PRIMARY KEY,
    vk_user_id BIGINT NOT NULL REFERENCES vk_contacts(vk_user_id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""",
"""
CREATE TABLE IF NOT EXISTS telegram_forum_topics (
    vk_user_id BIGINT PRIMARY KEY REFERENCES vk_contacts(vk_user_id),
    message_thread_id BIGINT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""",
"""
CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
""",
"""
CREATE TABLE IF NOT EXISTS webhook_events (
    provider TEXT NOT NULL,
    event_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('processing', 'sent', 'failed')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (provider, event_id)
);
""",
"""
CREATE TABLE IF NOT EXISTS webhook_queue (
    queue_id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    event_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued', 'processing', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (provider, event_id)
);
""",
"""
CREATE INDEX IF NOT EXISTS webhook_queue_provider_order
    ON webhook_queue (provider, queue_id) WHERE state IN ('queued', 'processing');
""",
"""
CREATE INDEX IF NOT EXISTS webhook_queue_terminal_age
    ON webhook_queue (updated_at) WHERE state IN ('sent', 'failed');
"""
)


def connect():
    database_url = os.getenv('DATABASE_URL', '').strip()
    if not database_url:
        raise RuntimeError('DATABASE_URL is not configured')
    return psycopg.connect(database_url, connect_timeout=5, row_factory=dict_row)


def initialize():
    with connect() as connection:
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)


def get_setting(key):
    with connect() as connection:
        result = connection.execute(
            'SELECT value FROM bot_settings WHERE key = %s',
            (key,)
        ).fetchone()
    return result['value'] if result else None


def save_setting(key, value):
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO bot_settings (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (key, str(value))
        )


def register_consent(vk_user_id, display_name):
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO vk_contacts (vk_user_id, display_name, consented_at, revoked_at)
            VALUES (%s, %s, NOW(), NULL)
            ON CONFLICT (vk_user_id) DO UPDATE
            SET display_name = EXCLUDED.display_name,
                consented_at = NOW(),
                revoked_at = NULL
            """,
            (vk_user_id, display_name)
        )


def revoke_consent(vk_user_id):
    with connect() as connection:
        with connection.transaction():
            result = connection.execute(
                "UPDATE vk_contacts SET revoked_at = NOW() WHERE vk_user_id = %s AND revoked_at IS NULL",
                (vk_user_id)
            )
            connection.execute(
                "DELETE FROM telegram_selection WHERE vk_user_id = %s",
                (vk_user_id)
            )
    return result.rowcount > 0


def list_consented_contacts():
    with connect() as connection:
        return connection.execute(
            "SELECT vk_user_id, display_name FROM vk_contacts WHERE revoked_at IS NULL ORDER BY display_name"
        ).fetchall()


def select_contact(owner_id, vk_user_id):
    with connect() as connection:
        with connection.transaction():
            contact = connection.execute(
                "SELECT vk_user_id FROM vk_contacts WHERE vk_user_id = %s AND revoked_at IS NULL FOR UPDATE",
                (vk_user_id)
            ).fetchone()
            if contact is None:
                return False
            connection.execute(
                """
                INSERT INTO telegram_selection (owner_id, vk_user_id, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (owner_id) DO UPDATE
                SET vk_user_id = EXCLUDED.vk_user_id, updated_at = NOW()
                """,
                (owner_id, vk_user_id)
            )
    return True


def get_selected_contact(owner_id):
    with connect() as connection:
        return connection.execute(
            """
            SELECT c.vk_user_id, c.display_name
            FROM telegram_selection AS s
            JOIN vk_contacts AS c ON c.vk_user_id = s.vk_user_id
            WHERE s.owner_id = %s AND c.revoked_at IS NULL
            """,
            (owner_id)
        ).fetchone()


def get_forum_topic(vk_user_id):
    with connect() as connection:
        return connection.execute(
            """
            SELECT message_thread_id
            FROM telegram_forum_topics
            WHERE vk_user_id = %s
            """,
            (vk_user_id)
        ).fetchone()


def save_forum_topic(vk_user_id, message_thread_id):
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO telegram_forum_topics (vk_user_id, message_thread_id)
            VALUES (%s, %s)
            ON CONFLICT (vk_user_id) DO UPDATE
            SET message_thread_id = EXCLUDED.message_thread_id
            """,
            (vk_user_id, message_thread_id)
        )


def get_forum_contact(message_thread_id):
    with connect() as connection:
        return connection.execute(
            """
            SELECT c.vk_user_id, c.display_name
            FROM telegram_forum_topics AS t
            JOIN vk_contacts AS c ON c.vk_user_id = t.vk_user_id
            WHERE t.message_thread_id = %s AND c.revoked_at IS NULL
            """,
            (message_thread_id)
        ).fetchone()


def get_forum_contact_by_vk_user(vk_user_id):
    with connect() as connection:
        return connection.execute(
            """
            SELECT c.vk_user_id, c.display_name, t.message_thread_id
            FROM telegram_forum_topics AS t
            JOIN vk_contacts AS c ON c.vk_user_id = t.vk_user_id
            WHERE t.vk_user_id = %s AND c.revoked_at IS NULL
            """,
            (vk_user_id)
        ).fetchone()


def clear_selection(owner_id):
    with connect() as connection:
        connection.execute("DELETE FROM telegram_selection WHERE owner_id = %s", (owner_id,))


def claim_event(provider, event_id):
    with connect() as connection:
        with connection.transaction():
            connection.execute(
                "DELETE FROM webhook_events WHERE updated_at < NOW() - INTERVAL '30 days'"
            )
            inserted = connection.execute(
                """
                INSERT INTO webhook_events (provider, event_id, state)
                VALUES (%s, %s, 'processing')
                ON CONFLICT DO NOTHING
                RETURNING event_id
                """,
                (provider, str(event_id))
            ).fetchone()
            if inserted is not None:
                return True
            reclaimed = connection.execute(
                """
                UPDATE webhook_events
                SET state = 'processing', updated_at = NOW()
                WHERE provider = %s AND event_id = %s
                  AND (state = 'failed' OR
                       (state = 'processing' AND updated_at < NOW() - INTERVAL '5 minutes'))
                RETURNING event_id
                """,
                (provider, str(event_id))
            ).fetchone()
            if reclaimed is not None:
                return True
            existing = connection.execute(
                'SELECT state FROM webhook_events WHERE provider = %s AND event_id = %s',
                (provider, str(event_id)),
            ).fetchone()
            if existing is None:
                return True
            return False if existing['state'] == 'sent' else None


def enqueue_webhook_event(provider, event_id, payload):
    message = payload.get('message') if isinstance(payload, dict) else None
    is_media_group = (
        provider == 'telegram'
        and isinstance(message, dict)
        and isinstance(message.get('media_group_id'), str)
    )
    delay_seconds = TELEGRAM_MEDIA_GROUP_DEBOUNCE_SECONDS if is_media_group else 0
    with connect() as connection:
        connection.execute(
            """
            DELETE FROM webhook_queue
            WHERE state IN ('sent', 'failed') AND updated_at < NOW() - INTERVAL '30 days'
            """
        )
        inserted = connection.execute(
            """
            INSERT INTO webhook_queue (provider, event_id, payload, state, available_at)
            VALUES (%s, %s, %s, 'queued', NOW() + (%s * INTERVAL '1 second'))
            ON CONFLICT (provider, event_id) DO NOTHING
            RETURNING queue_id
            """,
            (provider, str(event_id), Jsonb(payload), delay_seconds)
        ).fetchone()
        if inserted is not None and is_media_group:
            connection.execute(
                """
                UPDATE webhook_queue
                SET available_at = NOW() + (%s * INTERVAL '1 second')
                WHERE provider = %s AND state = 'queued'
                  AND payload->'message'->>'media_group_id' = %s
                """,
                (delay_seconds, provider, message['media_group_id'])
            )
    return inserted is not None


def claim_next_webhook_events(provider):
    with connect() as connection:
        with connection.transaction():
            connection.execute(
                """
                UPDATE webhook_queue
                SET state = 'queued', available_at = NOW(), updated_at = NOW()
                WHERE provider = %s AND state = 'processing'
                  AND updated_at < NOW() - INTERVAL '5 minutes'
                """,
                (provider)
            )
            first = connection.execute(
                """
                SELECT q.queue_id, q.event_id, q.payload
                FROM webhook_queue AS q
                WHERE q.provider = %s AND q.state = 'queued' AND q.available_at <= NOW()
                  AND NOT EXISTS (
                      SELECT 1 FROM webhook_queue AS earlier
                      WHERE earlier.provider = q.provider
                        AND earlier.queue_id < q.queue_id
                        AND earlier.state IN ('queued', 'processing')
                  )
                ORDER BY q.queue_id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                (provider)
            ).fetchone()
            if first is None:
                return []

            message = first['payload'].get('message') if isinstance(first['payload'], dict) else None
            media_group_id = message.get('media_group_id') if isinstance(message, dict) else None
            if provider == 'telegram' and isinstance(media_group_id, str):
                queued_rows = connection.execute(
                    """
                    SELECT queue_id, event_id, payload
                    FROM webhook_queue
                    WHERE provider = %s AND state = 'queued' AND available_at <= NOW()
                      AND queue_id >= %s
                    ORDER BY queue_id
                    FOR UPDATE
                    """,
                    (provider, first['queue_id'])
                ).fetchall()
                rows = []
                for row in queued_rows:
                    row_message = row['payload'].get('message')
                    if not isinstance(row_message, dict) or row_message.get('media_group_id') != media_group_id:
                        break
                    rows.append(row)
            else:
                rows = [first]

            queue_ids = [row['queue_id'] for row in rows]
            claimed = connection.execute(
                """
                UPDATE webhook_queue
                SET state = 'processing', attempts = attempts + 1, updated_at = NOW()
                WHERE queue_id = ANY(%s)
                RETURNING queue_id, event_id, payload, attempts
                """,
                (queue_ids)
            ).fetchall()
            return sorted(claimed, key=lambda row: row['queue_id'])


def finish_webhook_events(queue_ids, succeeded):
    if not queue_ids:
        return
    with connect() as connection:
        if succeeded:
            connection.execute(
                """
                UPDATE webhook_queue SET state = 'sent', updated_at = NOW()
                WHERE queue_id = ANY(%s) AND state = 'processing'
                """,
                (queue_ids,)
            )
        else:
            connection.execute(
                """
                UPDATE webhook_queue
                SET state = CASE WHEN attempts >= 5 THEN 'failed' ELSE 'queued' END,
                    available_at = NOW() +
                        (LEAST(60, POWER(2, GREATEST(attempts - 1, 0))) * INTERVAL '1 second'),
                    updated_at = NOW()
                WHERE queue_id = ANY(%s) AND state = 'processing'
                """,
                (queue_ids)
            )


def finish_event(provider, event_id, state):
    if state not in {'sent', 'failed'}:
        raise ValueError('Invalid webhook event state')
    with connect() as connection:
        connection.execute(
            """
            UPDATE webhook_events SET state = %s, updated_at = NOW()
            WHERE provider = %s AND event_id = %s
            """,
            (state, provider, str(event_id))
        )