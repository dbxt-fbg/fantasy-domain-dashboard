"""
Direct Jira API collector using REST API with API token authentication.
"""

import os
import logging
import requests
from datetime import datetime
from typing import Dict, Any, List
from pathlib import Path

from database.schema import get_connection

logger = logging.getLogger(__name__)


class JiraAPICollector:
    """Collects metrics from Jira via direct REST API calls."""

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Jira API collector.

        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.cloud_id = config['jira']['cloud_id']
        self.sprint_prefix = config['jira']['sprint_prefix']
        self.story_points_field = config['jira']['story_points_field']
        self.story_points_fallback_fields = config['jira'].get('story_points_fallback_fields', [])
        self.db_path = config['database']['path']

        # Load credentials from environment
        self._load_credentials()

        # Set up base URL and auth
        # For Atlassian Cloud, use the domain directly (not cloud ID)
        self.base_url = f"https://{self.cloud_id}/rest/api/3"
        self.session = requests.Session()
        self.session.auth = (self.email, self.api_token)
        self.session.headers.update({
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        })

    def _load_credentials(self):
        """Load Jira credentials from environment variables or .env file."""
        # Try to load from .env file if it exists
        env_file = Path(__file__).parent.parent.parent / "config" / ".env"
        if env_file.exists():
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        os.environ[key.strip()] = value.strip()

        # Get credentials
        self.email = os.environ.get('JIRA_EMAIL')
        self.api_token = os.environ.get('JIRA_API_TOKEN')

        if not self.email or not self.api_token:
            raise ValueError(
                "Jira credentials not found. Please set JIRA_EMAIL and JIRA_API_TOKEN "
                "environment variables or create config/.env file"
            )

        logger.info(f"Loaded Jira credentials for {self.email}")

    def search_issues(self, jql: str, fields: List[str], max_results: int = 100,
                      expand: List[str] = None) -> Dict[str, Any]:
        """
        Search Jira issues using JQL with pagination to get all results.

        Args:
            jql: JQL query string
            fields: List of field names to retrieve
            max_results: Maximum total number of results to fetch (not per page)
            expand: Optional list of expansions (e.g. ['changelog']) — only the
                past-sprint backfill needs the changelog so we keep it off by
                default to avoid blowing up payload size.

        Returns:
            Dict with all issues and metadata
        """
        # Use the new /search/jql endpoint (as of 2024)
        url = f"{self.base_url}/search/jql"

        all_issues = []
        start_at = 0
        total = None
        page_size = 100  # Jira max per page

        logger.info(f"Searching Jira with JQL: {jql[:100]}...")
        logger.info(f"URL: {url}")
        logger.info(f"Will fetch up to {max_results} total results")

        while len(all_issues) < max_results:
            params = {
                'jql': jql,
                'fields': ','.join(fields),
                'maxResults': page_size,
                'startAt': start_at
            }
            if expand:
                params['expand'] = ','.join(expand)

            logger.info(f"Fetching page starting at {start_at}, page size {page_size}")

            try:
                response = self.session.get(url, params=params, timeout=30)
                response.raise_for_status()
                data = response.json()

                # First page - get total count
                if total is None:
                    total = data.get('total', 0)
                    if total > 0:
                        logger.info(f"Found {total} total issues, fetching in pages of {page_size}")

                issues = data.get('issues', [])
                all_issues.extend(issues)

                logger.info(f"Got {len(issues)} issues in this page, total so far: {len(all_issues)}")

                # If we got fewer issues than page size, we've reached the end
                if len(issues) < page_size:
                    logger.info(f"Fetched all available issues: {len(all_issues)} (last page had {len(issues)} issues)")
                    break

                # If we got zero issues, stop
                if len(issues) == 0:
                    logger.info(f"No more issues, stopping at {len(all_issues)}")
                    break

                # If we've reached max_results limit, stop
                if len(all_issues) >= max_results:
                    logger.info(f"Reached max_results limit of {max_results}")
                    break

                # Move to next page
                start_at += page_size

            except requests.exceptions.RequestException as e:
                logger.error(f"Failed to search Jira issues: {e}")
                raise

        # Jira v3 /search/jql no longer returns `total` in the response — it
        # only returns the page of issues plus a nextPageToken. If we never
        # captured one above, fall back to len(all_issues) so callers that
        # only need "did we find anything?" (e.g. hygiene's child-count
        # verification) don't get fooled by a missing key.
        if total is None:
            total = len(all_issues)
        return {
            'issues': all_issues,
            'total': total,
            'startAt': 0,
            'maxResults': len(all_issues)
        }

    def collect_sprint_data(self) -> Dict[str, Any]:
        """
        Collect all data for active and future sprints.

        First fetches sprint metadata using openSprints/futureSprints to identify which sprints to query,
        then fetches ALL tickets associated with those specific sprint IDs (not just active ones).

        Returns:
            Dict with sprint data and tickets
        """
        logger.info("Collecting sprint data from Jira (active and future sprints)")

        # First, get sprint IDs from a sample query to identify which sprints are active/future
        jql_sample = f"(sprint in openSprints() OR sprint in futureSprints()) AND project = {self.sprint_prefix} ORDER BY created DESC"
        fields = [
            'summary', 'status', 'assignee', 'issuetype', 'priority',
            'created', 'updated', self.story_points_field, 'customfield_10020'
        ]
        # Add fallback story points fields
        fields.extend(self.story_points_fallback_fields)

        try:
            # Get sample to extract sprint IDs
            logger.info("Fetching sample to identify active/future sprint IDs...")
            sample_data = self.search_issues(jql_sample, fields, max_results=10)

            # Extract sprint IDs from the sample
            sprint_ids = set()
            for issue in sample_data.get('issues', []):
                sprint_list = issue['fields'].get('customfield_10020', [])
                for sprint_data in sprint_list:
                    sprint_id = sprint_data.get('id')
                    sprint_name = sprint_data.get('name', '')
                    state = sprint_data.get('state', '').lower()

                    # Only include active/future FNTSY sprints
                    if sprint_id and state in ['active', 'future'] and sprint_name.startswith(self.sprint_prefix):
                        sprint_ids.add(sprint_id)

            if not sprint_ids:
                logger.warning("No active or future sprints found")
                return {'issues': []}

            logger.info(f"Found sprint IDs: {sorted(sprint_ids)}")

            # Query for tickets in these specific sprints
            # Strategy: Fetch epics separately first, then stories/tasks
            # This ensures we get all epics even if we hit the max_results limit
            sprint_clauses = ' OR '.join([f'sprint = {sid}' for sid in sprint_ids])

            logger.info(f"Fetching epics first to ensure complete epic data...")
            # For epics, exclude Done to focus on active roadmap
            jql_epics = f"({sprint_clauses}) AND project = {self.sprint_prefix} AND type = Epic AND statusCategory != Done"
            epics_data = self.search_issues(jql_epics, fields, max_results=1000)
            logger.info(f"Retrieved {len(epics_data.get('issues', []))} epics")

            logger.info(f"Fetching stories and other issue types...")
            # For stories/tasks, include ALL statuses (including Done) to get accurate metrics
            jql_other = f"({sprint_clauses}) AND project = {self.sprint_prefix} AND type != Epic"
            other_data = self.search_issues(jql_other, fields, max_results=9000)
            logger.info(f"Retrieved {len(other_data.get('issues', []))} other issues")

            # Combine the results
            all_issues = epics_data.get('issues', []) + other_data.get('issues', [])
            data = {
                'issues': all_issues,
                'total': len(all_issues),
                'startAt': 0,
                'maxResults': len(all_issues)
            }
            logger.info(f"Total combined: {len(all_issues)} issues")
            logger.info(f"Retrieved {len(data.get('issues', []))} issues from Jira")
            return data
        except Exception as e:
            logger.error(f"Failed to collect sprint data: {e}")
            raise

    def store_sprint_snapshot(self, sprint_id: int, total_stories: int,
                              completed_stories: int, in_progress_stories: int) -> None:
        """
        Store daily sprint snapshot.

        Args:
            sprint_id: Internal sprint ID
            total_stories: Total number of stories
            completed_stories: Number of completed stories
            in_progress_stories: Number of in-progress stories
        """
        conn = get_connection(self.db_path)
        cursor = conn.cursor()

        try:
            today = datetime.now().date().isoformat()
            timestamp = datetime.now().isoformat()

            open_stories = total_stories - completed_stories

            cursor.execute("""
                INSERT INTO sprint_snapshots (
                    sprint_id, snapshot_date, snapshot_timestamp,
                    total_story_points, completed_story_points, remaining_story_points,
                    total_tickets, open_tickets, closed_tickets, in_progress_tickets
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sprint_id, snapshot_date) DO UPDATE SET
                    snapshot_timestamp = excluded.snapshot_timestamp,
                    total_story_points = excluded.total_story_points,
                    completed_story_points = excluded.completed_story_points,
                    remaining_story_points = excluded.remaining_story_points,
                    total_tickets = excluded.total_tickets,
                    open_tickets = excluded.open_tickets,
                    closed_tickets = excluded.closed_tickets,
                    in_progress_tickets = excluded.in_progress_tickets
            """, (
                sprint_id, today, timestamp,
                0.0, 0.0, 0.0,  # Story points (not used)
                total_stories, open_stories, completed_stories, in_progress_stories
            ))

            conn.commit()
            logger.info(f"Stored snapshot: {completed_stories}/{total_stories} stories completed, {open_stories} open")

        finally:
            conn.close()
