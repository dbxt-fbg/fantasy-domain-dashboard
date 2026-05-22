-- Current sprint summary
SELECT
    s.sprint_name,
    ss.snapshot_date,
    ss.total_story_points,
    ss.completed_story_points,
    ss.remaining_story_points,
    ROUND(ss.completed_story_points * 100.0 / NULLIF(ss.total_story_points, 0), 1) as completion_percent,
    ss.total_tickets,
    ss.open_tickets,
    ss.closed_tickets,
    ss.in_progress_tickets
FROM sprint_snapshots ss
JOIN sprints s ON ss.sprint_id = s.sprint_id
WHERE s.state = 'active'
ORDER BY ss.snapshot_date DESC
LIMIT 1;
