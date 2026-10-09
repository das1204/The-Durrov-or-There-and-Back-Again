SELECT 
    queue_id,
    provider,
    event_id,
    state,
    attempts,
    created_at,
    available_at,
    COALESCE(
        payload->>'text',
        payload->'message'->>'text',
        '[No text / Media only]'
    ) AS message_text_preview,
    COALESCE(
        payload->>'from_id',
        payload->'message'->'from'->>'id'
    ) AS sender_id
FROM webhook_queue
WHERE state = 'failed'
    OR (state = 'processing' AND updated_at < NOW() - INTERVAL '5 minutes')
    OR (state = 'queued' AND available_at < NOW() - INTERVAL '10 minutes')
ORDER BY updated_at DESC
LIMIT 50;