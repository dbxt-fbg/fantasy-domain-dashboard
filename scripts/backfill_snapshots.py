#!/usr/bin/env python3
"""
Backfill historical snapshots for burndown charts.
Creates daily snapshots from sprint start date to today using linear interpolation.
"""

import sys
from pathlib import Path
from datetime import datetime, date, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import load_config
from database.schema import get_connection

def backfill_sprint_snapshots():
    """Backfill missing daily snapshots for active sprints."""
    config = load_config()
    db_path = config['database']['path']

    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        # Get active sprints
        cursor.execute("""
            SELECT sprint_id, sprint_name, start_date, end_date
            FROM sprints
            WHERE state = 'active'
        """)

        sprints = cursor.fetchall()

        for sprint_row in sprints:
            sprint_id = sprint_row[0]
            sprint_name = sprint_row[1]
            start_date_str = sprint_row[2]
            end_date_str = sprint_row[3]

            # Parse dates
            start_date = datetime.fromisoformat(start_date_str.replace('Z', '+00:00')).date()
            today = date.today()

            print(f"\nProcessing {sprint_name}")
            print(f"  Sprint start: {start_date}")
            print(f"  Today: {today}")

            # Get current snapshot (today's data)
            cursor.execute("""
                SELECT total_tickets, open_tickets, closed_tickets, in_progress_tickets,
                       total_story_points, completed_story_points, remaining_story_points
                FROM sprint_snapshots
                WHERE sprint_id = ? AND snapshot_date = ?
            """, (sprint_id, today.isoformat()))

            current_snapshot = cursor.fetchone()
            if not current_snapshot:
                print(f"  No current snapshot found, skipping")
                continue

            total_tickets = current_snapshot[0]
            current_open = current_snapshot[1]
            current_closed = current_snapshot[2]
            current_in_progress = current_snapshot[3]
            total_sp = current_snapshot[4]
            current_completed_sp = current_snapshot[5]
            current_remaining_sp = current_snapshot[6]

            print(f"  Current: {current_closed}/{total_tickets} closed, {current_completed_sp}/{total_sp} SP")

            # Calculate days elapsed
            days_elapsed = (today - start_date).days
            if days_elapsed <= 0:
                print(f"  Sprint hasn't started yet, skipping")
                continue

            # Assume linear burndown for backfill
            # Day 0 (sprint start): 0 closed
            # Today: current_closed
            closed_per_day = current_closed / days_elapsed if days_elapsed > 0 else 0
            sp_per_day = current_completed_sp / days_elapsed if days_elapsed > 0 else 0

            # Create snapshots for each day
            snapshots_created = 0
            for day_offset in range(days_elapsed):
                snapshot_date = start_date + timedelta(days=day_offset)

                # Check if snapshot already exists
                cursor.execute("""
                    SELECT snapshot_id FROM sprint_snapshots
                    WHERE sprint_id = ? AND snapshot_date = ?
                """, (sprint_id, snapshot_date.isoformat()))

                if cursor.fetchone():
                    continue  # Skip if exists

                # Calculate interpolated values
                closed = int(closed_per_day * day_offset)
                completed_sp = sp_per_day * day_offset
                remaining_sp = total_sp - completed_sp
                open_tickets = total_tickets - closed

                # Estimate in_progress (simple heuristic: 20% of remaining)
                in_progress = int(open_tickets * 0.2)

                # Insert snapshot
                cursor.execute("""
                    INSERT INTO sprint_snapshots (
                        sprint_id, snapshot_date, snapshot_timestamp,
                        total_story_points, completed_story_points, remaining_story_points,
                        total_tickets, open_tickets, closed_tickets, in_progress_tickets
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    sprint_id,
                    snapshot_date.isoformat(),
                    datetime.combine(snapshot_date, datetime.min.time()).isoformat(),
                    total_sp, completed_sp, remaining_sp,
                    total_tickets, open_tickets, closed, in_progress
                ))

                snapshots_created += 1

            conn.commit()
            print(f"  Created {snapshots_created} historical snapshots")

        print("\n✅ Backfill complete!")

    finally:
        conn.close()

if __name__ == "__main__":
    backfill_sprint_snapshots()
