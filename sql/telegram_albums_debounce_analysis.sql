SELECT 
    payload->'message'->>'media_group_id' AS media_group_id,
    COUNT(*) AS photos_in_album,
    MIN(created_at) AS first_photo_at,
    MAX(created_at) AS last_photo_at,
    MAX(created_at) - MIN(created_at) AS time_spread,
    STRING_AGG(DISTINCT state, ', ') AS final_states
FROM webhook_queue
WHERE provider = 'telegram'
    AND payload->'message'->>'media_group_id' IS NOT NULL
    AND created_at >= NOW() - INTERVAL '7 days'
GROUP BY payload->'message'->>'media_group_id'
HAVING COUNT(*) > 1
ORDER BY first_photo_at DESC
LIMIT 30;