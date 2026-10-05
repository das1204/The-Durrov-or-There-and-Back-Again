import os
import psycopg
from psycopg.rows import dict_row


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
CREATE TABLE IF NOT EXISTS webhook_events (
    provider TEXT NOT NULL,
    event_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('processing', 'sent', 'failed')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (provider, event_id)
);
""",
"""
CREATE TABLE IF NOT EXISTS outbound_rate_events (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""",
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
            (vk_user_id, display_name),
        )


def revoke_consent(vk_user_id):
    with connect() as connection:
        with connection.transaction():
            result = connection.execute(
                "UPDATE vk_contacts SET revoked_at = NOW() WHERE vk_user_id = %s AND revoked_at IS NULL",
                (vk_user_id,),
            )
            connection.execute(
                "DELETE FROM telegram_selection WHERE vk_user_id = %s",
                (vk_user_id,),
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
                (vk_user_id,),
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
                (owner_id, vk_user_id),
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
            (owner_id,),
        ).fetchone()


def get_forum_topic(vk_user_id):
    with connect() as connection:
        return connection.execute(
            """
            SELECT message_thread_id
            FROM telegram_forum_topics
            WHERE vk_user_id = %s
            """,
            (vk_user_id,),
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
            (vk_user_id, message_thread_id),
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
            (message_thread_id,),
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
            (vk_user_id,),
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
                (provider, str(event_id)),
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
                (provider, str(event_id)),
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


def reserve_outbound_message(owner_id):
    with connect() as connection:
        with connection.transaction():
            connection.execute('SELECT pg_advisory_xact_lock(%s)', (owner_id,))
            counts = connection.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '1 minute') AS minute_count,
                    COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '1 day') AS day_count
                FROM outbound_rate_events
                WHERE owner_id = %s
                """,
                (owner_id,),
            ).fetchone()
            if counts['minute_count'] >= 10 or counts['day_count'] >= 1_000:
                return False
            connection.execute(
                'INSERT INTO outbound_rate_events (owner_id) VALUES (%s)',
                (owner_id,),
            )
            connection.execute(
                "DELETE FROM outbound_rate_events WHERE owner_id = %s AND created_at < NOW() - INTERVAL '1 day'",
                (owner_id,),
            )
    return True


def finish_event(provider, event_id, state):
    if state not in {'sent', 'failed'}:
        raise ValueError('Invalid webhook event state')
    with connect() as connection:
        connection.execute(
            """
            UPDATE webhook_events SET state = %s, updated_at = NOW()
            WHERE provider = %s AND event_id = %s
            """,
            (state, provider, str(event_id)),
        )