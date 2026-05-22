"""
Google Calendar collector for 1-on-1 meetings.
"""

import logging
import pickle
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from database.schema import get_connection

logger = logging.getLogger(__name__)

# If modifying these scopes, delete the file token.pickle
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']


class CalendarCollector:
    """Collects recurring 1-on-1 meetings from Google Calendar."""

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Calendar collector.

        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.db_path = config['database']['path']
        self.team_members = config.get('team_members', [])
        self.credentials_path = Path(config['database']['path']).parent.parent / "config" / "google_credentials.json"
        self.token_path = Path(config['database']['path']).parent.parent / "config" / "token.pickle"
        self.service = None

    def authenticate(self):
        """Authenticate with Google Calendar API."""
        creds = None

        # The file token.pickle stores the user's access and refresh tokens
        if self.token_path.exists():
            with open(self.token_path, 'rb') as token:
                creds = pickle.load(token)

        # If there are no (valid) credentials available, let the user log in
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info("Refreshing expired credentials")
                creds.refresh(Request())
            else:
                logger.info("Starting OAuth flow")
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.credentials_path), SCOPES)
                creds = flow.run_local_server(port=0)

            # Save the credentials for the next run
            with open(self.token_path, 'wb') as token:
                pickle.dump(creds, token)

        self.service = build('calendar', 'v3', credentials=creds)
        logger.info("Successfully authenticated with Google Calendar")

    def collect_one_on_one_meetings(self) -> None:
        """Collect and store recurring 1-on-1 meetings."""
        if not self.service:
            self.authenticate()

        logger.info("Starting 1-on-1 meeting collection from Google Calendar")

        try:
            # Get events from the next 30 days
            now = datetime.utcnow().isoformat() + 'Z'
            time_max = (datetime.utcnow() + timedelta(days=30)).isoformat() + 'Z'

            # Get all events (recurring definitions)
            events_result = self.service.events().list(
                calendarId='primary',
                timeMin=now,
                timeMax=time_max,
                singleEvents=False  # Get recurring event definitions
            ).execute()

            events = events_result.get('items', [])
            logger.info(f"Found {len(events)} calendar events")

            # Filter for recurring events that look like 1-on-1s
            one_on_ones = self._filter_one_on_one_meetings(events)
            logger.info(f"Identified {len(one_on_ones)} potential 1-on-1 meetings")

            # Store in database
            self._store_meetings(one_on_ones)

            logger.info("1-on-1 meeting collection complete")

        except HttpError as error:
            logger.error(f"An error occurred: {error}")
            raise

    def _filter_one_on_one_meetings(self, events: List[Dict]) -> List[Dict]:
        """
        Filter events to find recurring 1-on-1 meetings.

        Args:
            events: List of calendar events

        Returns:
            List of filtered 1-on-1 meeting events
        """
        one_on_ones = []
        team_names = {member['name'].lower() for member in self.team_members}

        for event in events:
            summary = event.get('summary', '').lower()

            # Check if it's recurring
            if 'recurrence' not in event:
                continue

            # Check if it's a 1-on-1 (contains team member name or common keywords)
            is_one_on_one = False
            matched_name = None

            # Look for team member names in the event title
            for member in self.team_members:
                name_parts = member['name'].lower().split()
                # Check if any part of the name appears in the summary
                if any(part in summary for part in name_parts if len(part) > 2):
                    is_one_on_one = True
                    matched_name = member['name']
                    break

            # Also check for common 1-on-1 keywords
            if not is_one_on_one:
                one_on_one_keywords = ['1-on-1', '1:1', 'one on one', 'check-in', 'sync']
                if any(keyword in summary for keyword in one_on_one_keywords):
                    # Try to extract name from attendees
                    attendees = event.get('attendees', [])
                    if len(attendees) == 1:  # Just you and one other person
                        attendee_email = attendees[0].get('email', '').lower()
                        # Try to match attendee to team member
                        for member in self.team_members:
                            name_parts = member['name'].lower().split()
                            if any(part in attendee_email for part in name_parts if len(part) > 2):
                                is_one_on_one = True
                                matched_name = member['name']
                                break

            if is_one_on_one and matched_name:
                event['matched_team_member'] = matched_name
                one_on_ones.append(event)
                logger.info(f"Found 1-on-1 with {matched_name}: {event.get('summary')}")

        return one_on_ones

    def _parse_recurrence_rule(self, recurrence: List[str]) -> Dict[str, Any]:
        """
        Parse RRULE to extract day of week and frequency.

        Args:
            recurrence: List of recurrence rules

        Returns:
            Dict with parsed recurrence info
        """
        if not recurrence:
            return {}

        rrule = recurrence[0]  # Usually just one rule

        result = {
            'recurrence_rule': rrule,
            'day_of_week': None,
            'frequency': 'weekly'
        }

        # Parse RRULE format: RRULE:FREQ=WEEKLY;BYDAY=MO
        if 'BYDAY=' in rrule:
            day_part = rrule.split('BYDAY=')[1].split(';')[0]
            day_map = {
                'MO': 'Monday',
                'TU': 'Tuesday',
                'WE': 'Wednesday',
                'TH': 'Thursday',
                'FR': 'Friday',
                'SA': 'Saturday',
                'SU': 'Sunday'
            }
            result['day_of_week'] = day_map.get(day_part, day_part)

        if 'FREQ=' in rrule:
            freq = rrule.split('FREQ=')[1].split(';')[0]
            result['frequency'] = freq.lower()

        return result

    def _get_next_occurrence(self, event: Dict) -> Optional[str]:
        """
        Get the next occurrence of a recurring event.

        Args:
            event: Calendar event

        Returns:
            ISO format datetime string of next occurrence
        """
        try:
            # Get event instances (actual occurrences)
            now = datetime.utcnow().isoformat() + 'Z'
            time_max = (datetime.utcnow() + timedelta(days=30)).isoformat() + 'Z'

            instances = self.service.events().instances(
                calendarId='primary',
                eventId=event['id'],
                timeMin=now,
                timeMax=time_max,
                maxResults=1
            ).execute()

            if instances.get('items'):
                next_event = instances['items'][0]
                start = next_event.get('start', {})
                return start.get('dateTime', start.get('date'))

        except HttpError as e:
            logger.warning(f"Could not get next occurrence for event {event.get('id')}: {e}")

        return None

    def _store_meetings(self, meetings: List[Dict]) -> None:
        """Store 1-on-1 meetings in database.

        Builds all row tuples up front (no API/DB mixed work), then flushes
        under a single connection with retry in case another writer (hygiene,
        QA, collector) is mid-commit and holds the write lock longer than the
        default busy_timeout.
        """
        now = datetime.now().isoformat()

        # Build rows first — pure Python, no DB lock needed.
        rows = []
        for event in meetings:
            team_member_name = event.get('matched_team_member')
            if not team_member_name:
                continue

            team_member = next(
                (m for m in self.team_members if m['name'] == team_member_name),
                None,
            )
            if not team_member:
                continue

            recurrence_info = self._parse_recurrence_rule(event.get('recurrence', []))

            start = event.get('start', {})
            start_time = start.get('dateTime', start.get('date', ''))

            time_of_day = None
            if start_time:
                try:
                    dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                    time_of_day = dt.strftime('%I:%M %p')
                except Exception:
                    pass

            duration_minutes = None
            if start_time and 'end' in event:
                end_time = event['end'].get('dateTime', event['end'].get('date', ''))
                try:
                    start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                    end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
                    duration_minutes = int((end_dt - start_dt).total_seconds() / 60)
                except Exception:
                    pass

            next_occurrence = self._get_next_occurrence(event)

            rows.append((
                team_member_name,
                team_member.get('jira_account_id'),
                team_member.get('github_username'),
                event.get('id'),
                event.get('summary'),
                recurrence_info.get('recurrence_rule'),
                recurrence_info.get('day_of_week'),
                time_of_day,
                duration_minutes,
                next_occurrence,
                now,
            ))

        # Flush with retry (matches the pattern used by github_collector).
        # Total budget ~3 minutes — more than enough to outlast any hygiene
        # or QA transaction on this DB.
        backoffs = [5, 10, 20, 40, 60, 60]
        last_err = None
        for attempt, wait in enumerate(backoffs):
            try:
                conn = get_connection(self.db_path)
                cursor = conn.cursor()
                try:
                    cursor.execute("DELETE FROM one_on_one_meetings")
                    if rows:
                        cursor.executemany("""
                            INSERT INTO one_on_one_meetings (
                                developer_name, jira_account_id, github_username,
                                event_id, summary, recurrence_rule, day_of_week,
                                time_of_day, duration_minutes, next_occurrence, last_synced_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(developer_name) DO UPDATE SET
                                event_id = excluded.event_id,
                                summary = excluded.summary,
                                recurrence_rule = excluded.recurrence_rule,
                                day_of_week = excluded.day_of_week,
                                time_of_day = excluded.time_of_day,
                                duration_minutes = excluded.duration_minutes,
                                next_occurrence = excluded.next_occurrence,
                                last_synced_at = excluded.last_synced_at
                        """, rows)
                    conn.commit()
                    logger.info(
                        "Stored %d 1-on-1 meetings in database (attempt %d)",
                        len(rows), attempt + 1,
                    )
                    return
                finally:
                    conn.close()
            except Exception as e:
                last_err = e
                logger.warning(
                    "Calendar DB write attempt %d failed (%s); retrying in %ds…",
                    attempt + 1, e, wait,
                )
                import time as _time
                _time.sleep(wait)
        raise RuntimeError(
            f"Calendar sync failed to persist {len(rows)} meetings after "
            f"{len(backoffs)} attempts: {last_err}"
        )
