SELECT 
    vk_user_id,
    display_name,
    consented_at AS registered_at,
    revoked_at AS churned_at,
    ROUND(EXTRACT(EPOCH FROM (revoked_at - consented_at)) / 3600, 2) AS hours_active_before_churn
FROM vk_contacts
WHERE revoked_at IS NOT NULL
ORDER BY revoked_at DESC;