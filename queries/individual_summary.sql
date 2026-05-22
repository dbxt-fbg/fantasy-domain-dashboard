-- Individual developer summary for current sprint
SELECT
    ds.developer_name,
    ds.assigned_story_points,
    ds.completed_story_points,
    ds.remaining_story_points,
    ROUND(ds.completed_story_points * 100.0 / NULLIF(ds.assigned_story_points, 0), 1) as completion_percent,
    ds.tickets_completed,
    ds.tickets_in_progress,
    ds.tickets_todo,
    gps.open_pr_count,
    (
        SELECT AVG(CAST((julianday(gp.merged_at) - julianday(gp.created_at)) * 24 AS REAL))
        FROM github_prs gp
        WHERE gp.author_github_username = (
            SELECT github_username
            FROM (SELECT 'fake' as github_username)
        )
        AND gp.merged_at IS NOT NULL
        AND date(gp.merged_at) >= date('now', '-30 days')
    ) as avg_hours_to_merge
FROM developer_snapshots ds
JOIN sprints s ON ds.sprint_id = s.sprint_id
LEFT JOIN github_pr_snapshots gps ON
    gps.snapshot_timestamp = (
        SELECT MAX(snapshot_timestamp)
        FROM github_pr_snapshots
    )
WHERE s.state = 'active'
    AND ds.snapshot_date = (
        SELECT MAX(snapshot_date)
        FROM developer_snapshots
        WHERE sprint_id = ds.sprint_id
    )
ORDER BY ds.developer_name;
