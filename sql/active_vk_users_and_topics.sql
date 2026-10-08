SELECT 
    c.vk_user_id,
    c.display_name,
    c.consented_at AS registered_at,
    t.message_thread_id AS tg_topic_id,
    t.created_at AS topic_created_at
FROM vk_contacts AS c
LEFT JOIN telegram_forum_topics AS t ON c.vk_user_id = t.vk_user_id
WHERE c.revoked_at IS NULL
ORDER BY c.consented_at DESC;