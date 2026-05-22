"""
GitHub metrics collector using GitHub CLI.
"""

import logging
import json
import subprocess
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from database.schema import get_connection
from models.metrics import GitHubPR, GitHubPRMetrics

logger = logging.getLogger(__name__)


class GitHubCollector:
    """Collects metrics from GitHub via gh CLI."""

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize GitHub collector.

        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.organization = config['github']['organization']
        self.team_slug = config['github'].get('team_slug')
        self.team_members = config.get('team_members', [])
        self.lookback_days = config['collection'].get('github_pr_lookback_days', 90)
        self.db_path = config['database']['path']

    def _open_db(self):
        """Open a DB connection with a busy-wait timeout so parallel agents
        (e.g. the hygiene cron) don't kill our writes with 'database is locked'.
        """
        conn = get_connection(self.db_path)
        # Wait up to 30s for another writer to release the lock
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def collect_pr_metrics(self) -> None:
        """Collect and store PR metrics for all team members."""
        logger.info("Starting GitHub PR metrics collection")

        if not self.team_members:
            logger.warning("No team members configured, skipping GitHub collection")
            return

        try:
            for member in self.team_members:
                github_username = member.get('github_username')
                if not github_username:
                    logger.warning(f"No GitHub username for {member.get('name')}")
                    continue

                logger.info(f"Collecting PR metrics for {github_username}")

                try:
                    # Get open PRs
                    open_prs = self._get_open_prs(github_username)
                    logger.info(f"Found {len(open_prs)} open PRs")

                    # Get recently merged PRs for time-to-merge calculation
                    merged_prs = self._get_merged_prs(github_username, days=self.lookback_days)
                    logger.info(f"Found {len(merged_prs)} merged PRs in last {self.lookback_days} days")

                    # Calculate metrics
                    metrics = self._calculate_pr_metrics(
                        github_username,
                        member.get('name', github_username),
                        open_prs,
                        merged_prs
                    )

                    # Store snapshot
                    self._store_pr_snapshot(metrics)

                    # Store individual PRs
                    self._store_prs(open_prs + merged_prs)

                    logger.info(
                        f"{github_username}: {metrics.open_pr_count} open PRs, "
                        f"avg {metrics.avg_time_to_merge_hours}h to merge"
                    )

                    # Collect reviews + PR comments the member left on others' PRs
                    review_counts = self._collect_reviews_and_comments(
                        github_username, days=self.lookback_days
                    )
                    logger.info(
                        f"{github_username}: {review_counts['approvals']} approvals, "
                        f"{review_counts['changes_requested']} changes req, "
                        f"{review_counts['review_comments']} review comments, "
                        f"{review_counts['pr_comments']} PR comments"
                    )

                except Exception as e:
                    logger.error(f"Failed to collect PRs for {github_username}: {e}", exc_info=True)
                    continue

                # Small delay to avoid rate limiting
                time.sleep(0.5)

        except Exception as e:
            logger.error(f"Failed to collect GitHub metrics: {e}", exc_info=True)
            raise

    def _get_open_prs(self, username: str) -> List[GitHubPR]:
        """
        Get open PRs for a user.

        Args:
            username: GitHub username

        Returns:
            List of GitHubPR objects
        """
        try:
            # Use gh search to avoid needing to be in a git repo
            cmd = [
                'gh', 'search', 'prs',
                '--author', username,
                '--owner', self.organization,
                '--state', 'open',
                '--json', 'number,title,createdAt,updatedAt,url',
                '--limit', '1000'
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True
            )

            prs_data = json.loads(result.stdout)

            prs = []
            for pr_data in prs_data:
                # Extract repository from URL
                url = pr_data.get('url', '')
                repo_full_name = 'unknown'
                if 'github.com/' in url:
                    # URL format: https://github.com/owner/repo/pull/123
                    parts = url.split('github.com/')[1].split('/')
                    if len(parts) >= 2:
                        repo_full_name = f"{parts[0]}/{parts[1]}"

                # Fetch PR details to get additions/deletions
                lines_added, lines_deleted = self._get_pr_size(repo_full_name, pr_data['number'])

                pr = GitHubPR(
                    pr_number=pr_data['number'],
                    repository=repo_full_name,
                    author_github_username=username,
                    title=pr_data.get('title', ''),
                    state='open',
                    created_at=pr_data['createdAt'],
                    updated_at=pr_data.get('updatedAt'),
                    pr_url=pr_data.get('url'),
                    lines_added=lines_added,
                    lines_deleted=lines_deleted
                )
                prs.append(pr)

            return prs

        except subprocess.CalledProcessError as e:
            # Check if it's an auth or rate limit error - usually transient
            error_str = str(e.stderr)
            if '401' in error_str or 'authentication' in error_str.lower():
                logger.warning(f"gh CLI authentication issue for {username}. This is usually transient and will resolve on next run.")
            elif '403' in error_str or 'rate limit' in error_str.lower():
                logger.warning(f"gh CLI rate limit for {username}. Will retry on next collection cycle.")
            else:
                logger.error(f"gh CLI error for {username}: {e.stderr}")
            return []
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse gh CLI output: {e}")
            return []

    def _get_merged_prs(self, username: str, days: int = 90) -> List[GitHubPR]:
        """
        Get merged PRs for a user within the last N days.

        Args:
            username: GitHub username
            days: Number of days to look back

        Returns:
            List of GitHubPR objects
        """
        try:
            # Calculate cutoff date
            cutoff_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')

            # Use gh search prs with proper flags
            cmd = [
                'gh', 'search', 'prs',
                '--author', username,
                '--owner', self.organization,
                '--merged',
                '--merged-at', f'>={cutoff_date}',
                '--json', 'number,title,createdAt,updatedAt,closedAt,url',
                '--limit', '1000'
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True
            )

            prs_data = json.loads(result.stdout)

            prs = []
            for pr_data in prs_data:
                # For search results, extract repo from URL
                url = pr_data.get('url', '')
                repo_full_name = 'unknown'
                if 'github.com/' in url:
                    # URL format: https://github.com/owner/repo/pull/123
                    parts = url.split('github.com/')[1].split('/')
                    if len(parts) >= 2:
                        repo_full_name = f"{parts[0]}/{parts[1]}"

                # Fetch PR details to get additions/deletions
                lines_added, lines_deleted = self._get_pr_size(repo_full_name, pr_data['number'])

                pr = GitHubPR(
                    pr_number=pr_data['number'],
                    repository=repo_full_name,
                    author_github_username=username,
                    title=pr_data.get('title', ''),
                    state='merged',
                    created_at=pr_data['createdAt'],
                    updated_at=pr_data.get('updatedAt'),
                    merged_at=pr_data.get('closedAt'),  # Use closedAt as proxy for merged_at
                    closed_at=pr_data.get('closedAt'),
                    pr_url=pr_data.get('url'),
                    lines_added=lines_added,
                    lines_deleted=lines_deleted
                )
                prs.append(pr)

            return prs

        except subprocess.CalledProcessError as e:
            # Check if it's an auth or rate limit error - usually transient
            error_str = str(e.stderr)
            if '401' in error_str or 'authentication' in error_str.lower():
                logger.warning(f"gh CLI authentication issue for {username}. This is usually transient and will resolve on next run.")
            elif '403' in error_str or 'rate limit' in error_str.lower():
                logger.warning(f"gh CLI rate limit for {username}. Will retry on next collection cycle.")
            else:
                logger.error(f"gh CLI error for {username}: {e.stderr}")
            return []
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse gh CLI output: {e}")
            return []

    def _calculate_pr_metrics(
        self,
        github_username: str,
        developer_name: str,
        open_prs: List[GitHubPR],
        merged_prs: List[GitHubPR]
    ) -> GitHubPRMetrics:
        """
        Calculate PR metrics for a developer.

        Args:
            github_username: GitHub username
            developer_name: Developer's display name
            open_prs: List of open PRs
            merged_prs: List of merged PRs

        Returns:
            GitHubPRMetrics object
        """
        # Calculate average time to merge
        merge_times = []
        for pr in merged_prs:
            if pr.created_at and pr.merged_at:
                time_to_merge = self._calculate_time_to_merge(pr.created_at, pr.merged_at)
                if time_to_merge is not None:
                    merge_times.append(time_to_merge)

        avg_hours = sum(merge_times) / len(merge_times) if merge_times else None

        # Build PR details for open PRs
        pr_details = [
            {
                'number': pr.pr_number,
                'title': pr.title,
                'repository': pr.repository,
                'url': pr.pr_url,
                'created_at': pr.created_at
            }
            for pr in open_prs
        ]

        return GitHubPRMetrics(
            developer_github_username=github_username,
            developer_name=developer_name,
            open_pr_count=len(open_prs),
            pr_details=pr_details,
            avg_time_to_merge_hours=round(avg_hours, 1) if avg_hours else None,
            merged_pr_count=len(merged_prs)
        )

    def _get_pr_size(self, repository: str, pr_number: int) -> tuple:
        """
        Get PR size (additions and deletions), preferring the cached value.

        Each open PR previously triggered a `gh pr view` subprocess; with
        ~50 open PRs across the team that's 50 fork/exec calls per cycle and
        most of them re-fetched the same numbers. Now we look up the cached
        size in `github_prs` first and only hit the API for new/empty rows.

        Args:
            repository: Full repository name (owner/repo)
            pr_number: PR number

        Returns:
            Tuple of (lines_added, lines_deleted)
        """
        # Fast path: cached size from a previous run.
        try:
            with self._open_db() as conn:
                row = conn.execute(
                    "SELECT lines_added, lines_deleted FROM github_prs "
                    "WHERE repository = ? AND pr_number = ?",
                    (repository, pr_number),
                ).fetchone()
            if row and (row[0] or row[1]):
                return int(row[0] or 0), int(row[1] or 0)
        except Exception as e:
            logger.debug(f"PR size cache lookup failed for {repository}#{pr_number}: {e}")
            # Fall through to the API call.

        try:
            cmd = [
                'gh', 'pr', 'view', str(pr_number),
                '--repo', repository,
                '--json', 'additions,deletions'
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=5
            )

            data = json.loads(result.stdout)
            return data.get('additions', 0), data.get('deletions', 0)

        except (subprocess.CalledProcessError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            logger.debug(f"Could not fetch PR size for {repository}#{pr_number}: {e}")
            return 0, 0

    def _calculate_time_to_merge(self, created_at: str, merged_at: str) -> Optional[float]:
        """
        Calculate time from PR creation to merge in hours.

        Args:
            created_at: ISO timestamp of PR creation
            merged_at: ISO timestamp of PR merge

        Returns:
            Hours between timestamps, or None if calculation fails
        """
        try:
            created = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            merged = datetime.fromisoformat(merged_at.replace('Z', '+00:00'))
            delta = merged - created
            return delta.total_seconds() / 3600
        except Exception as e:
            logger.warning(f"Failed to calculate time to merge: {e}")
            return None

    def _store_pr_snapshot(self, metrics: GitHubPRMetrics) -> None:
        """Store PR metrics snapshot, retrying briefly if another agent holds the lock."""
        last_err = None
        for attempt in range(3):
            try:
                conn = self._open_db()
                try:
                    conn.execute("""
                        INSERT INTO github_pr_snapshots (
                            snapshot_timestamp, developer_github_username, developer_name,
                            open_pr_count, pr_details
                        ) VALUES (?, ?, ?, ?, ?)
                    """, (
                        datetime.now().isoformat(),
                        metrics.developer_github_username,
                        metrics.developer_name,
                        metrics.open_pr_count,
                        json.dumps(metrics.pr_details),
                    ))
                    conn.commit()
                    return
                finally:
                    conn.close()
            except Exception as e:
                last_err = e
                time.sleep(2 + attempt * 3)
        raise last_err

    def _store_prs(self, prs: List[GitHubPR]) -> None:
        """Store individual PR records."""
        conn = self._open_db()
        cursor = conn.cursor()

        try:
            now = datetime.now().isoformat()

            for pr in prs:
                cursor.execute("""
                    INSERT INTO github_prs (
                        pr_number, repository, author_github_username, title, state,
                        created_at, updated_at, merged_at, closed_at, pr_url,
                        lines_added, lines_deleted, first_seen_at, last_updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(repository, pr_number) DO UPDATE SET
                        state = excluded.state,
                        updated_at = excluded.updated_at,
                        merged_at = excluded.merged_at,
                        closed_at = excluded.closed_at,
                        lines_added = excluded.lines_added,
                        lines_deleted = excluded.lines_deleted,
                        last_updated_at = excluded.last_updated_at
                """, (
                    pr.pr_number, pr.repository, pr.author_github_username,
                    pr.title, pr.state, pr.created_at, pr.updated_at,
                    pr.merged_at, pr.closed_at, pr.pr_url,
                    pr.lines_added, pr.lines_deleted, now, now
                ))

            conn.commit()

        finally:
            conn.close()

    def _collect_reviews_and_comments(self, reviewer_username: str, days: int = 90) -> Dict[str, int]:
        """Collect reviews and PR-level comments left by a team member in the last N days.

        Uses `gh search prs --reviewed-by <user>` to find PRs the reviewer touched,
        then pulls per-PR reviews + issue-comments via the REST API. The filter by
        `submitted_at`/`created_at` is applied in Python because GitHub does not
        support it server-side on those endpoints.
        """
        cutoff = datetime.now() - timedelta(days=days)

        # Find PRs the user touched via three sources, unioned together:
        #
        #   1. `gh search prs --reviewed-by <user>`  — indexed review history
        #   2. `gh search prs --commenter <user>`    — indexed issue-comment history
        #   3. `gh api /users/<user>/events`         — last ~90d of events, no
        #                                              search-index lag
        #
        # GitHub's search index can lag by minutes-to-hours and has been
        # observed to miss recent reviews on cross-org PRs. The events feed
        # is the authoritative source for a user's own activity but only
        # covers ~300 events / 90 days. Union covers both bases.
        pr_refs: Dict[tuple, str] = {}  # (repo, number) -> pr_url
        for flag in ('--reviewed-by', '--commenter'):
            prs = self._search_prs_for_reviewer(reviewer_username, flag, days)
            for repo, number, url in prs:
                pr_refs[(repo, number)] = url

        for repo, number, url in self._events_pr_refs(reviewer_username, cutoff):
            pr_refs.setdefault((repo, number), url)

        counts = {
            'approvals': 0,
            'changes_requested': 0,
            'review_comments': 0,
            'pr_comments': 0,
        }

        # Accumulate all rows across all PRs for this member, then flush under a
        # single connection/commit at the end. The earlier per-insert approach
        # opened a new connection for every review and timed out whenever the
        # hygiene/QA cron held a write lock for more than busy_timeout.
        review_rows = []
        comment_rows = []
        now_iso = datetime.now().isoformat()

        for (repo, number), url in pr_refs.items():
            reviews = self._fetch_pr_reviews(repo, number)
            comments_per_review = self._count_inline_comments_per_review(repo, number)
            for rev in reviews:
                if rev.get('user', {}).get('login') != reviewer_username:
                    continue
                submitted_at = rev.get('submitted_at')
                if not submitted_at:
                    continue
                try:
                    ts = datetime.fromisoformat(submitted_at.replace('Z', '+00:00'))
                except ValueError:
                    continue
                if ts.replace(tzinfo=None) < cutoff:
                    continue

                state = rev.get('state', 'COMMENTED')
                inline_count = comments_per_review.get(rev.get('id'), 0)
                review_rows.append((repo, number, url, reviewer_username, state, inline_count, submitted_at, now_iso))

                if state == 'APPROVED':
                    counts['approvals'] += 1
                elif state == 'CHANGES_REQUESTED':
                    counts['changes_requested'] += 1
                counts['review_comments'] += inline_count

            issue_comments = self._fetch_pr_issue_comments(repo, number)
            for c in issue_comments:
                if c.get('user', {}).get('login') != reviewer_username:
                    continue
                created_at = c.get('created_at')
                if not created_at:
                    continue
                try:
                    ts = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                except ValueError:
                    continue
                if ts.replace(tzinfo=None) < cutoff:
                    continue

                cid = c.get('id')
                if cid is None:
                    continue
                comment_rows.append((cid, repo, number, url, reviewer_username, created_at, now_iso))
                counts['pr_comments'] += 1

        # Single connection, single commit, with retry in case the DB is
        # briefly locked by a concurrent agent.
        self._flush_reviews_and_comments(review_rows, comment_rows)

        return counts

    def _flush_reviews_and_comments(self, review_rows, comment_rows):
        """Write all review/comment rows for one member under a single commit.

        The hygiene/QA cron can hold a write lock for ~30-60s during its commit,
        so we retry for a few minutes rather than giving up quickly. INSERT OR
        IGNORE + the UNIQUE constraints make reruns safe.
        """
        if not review_rows and not comment_rows:
            return
        last_err = None
        # Backoff schedule in seconds — total budget ~3 minutes.
        backoffs = [5, 10, 20, 40, 60, 60]
        for attempt, wait in enumerate(backoffs):
            try:
                conn = self._open_db()
                try:
                    if review_rows:
                        conn.executemany(
                            """
                            INSERT OR IGNORE INTO github_reviews
                                (repository, pr_number, pr_url, reviewer_github_username,
                                 state, inline_comment_count, submitted_at, first_seen_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            review_rows,
                        )
                    if comment_rows:
                        conn.executemany(
                            """
                            INSERT OR IGNORE INTO github_pr_comments
                                (comment_id, repository, pr_number, pr_url,
                                 commenter_github_username, created_at, first_seen_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            comment_rows,
                        )
                    conn.commit()
                    logger.info(
                        "Persisted %d review rows + %d comment rows (attempt %d)",
                        len(review_rows), len(comment_rows), attempt + 1,
                    )
                    return
                finally:
                    conn.close()
            except Exception as e:
                last_err = e
                logger.warning(
                    "Persist attempt %d failed (%s); retrying in %ds…",
                    attempt + 1, e, wait,
                )
                time.sleep(wait)
        logger.error(
            "Failed to persist %d review / %d comment rows after %d attempts: %s",
            len(review_rows), len(comment_rows), len(backoffs), last_err,
        )

    def _search_prs_for_reviewer(self, username: str, flag: str, days: int) -> List[tuple]:
        """Return (repo, pr_number, pr_url) tuples for PRs touched by `username` via
        the given `gh search prs` flag (e.g. --reviewed-by or --commenter).
        """
        cutoff_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        try:
            cmd = [
                'gh', 'search', 'prs', flag, username,
                '--owner', self.organization,
                '--updated', f'>={cutoff_date}',
                '--json', 'number,url',
                '--limit', '1000',
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
            data = json.loads(result.stdout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            logger.warning(f"gh search prs {flag} {username} failed: {e}")
            return []

        out = []
        for pr in data:
            url = pr.get('url', '')
            if 'github.com/' not in url:
                continue
            parts = url.split('github.com/')[1].split('/')
            if len(parts) < 2:
                continue
            repo = f"{parts[0]}/{parts[1]}"
            out.append((repo, pr.get('number'), url))
        return out

    def _events_pr_refs(self, username: str, cutoff: datetime) -> List[tuple]:
        """Return (repo, pr_number, pr_url) tuples for PRs where `username`
        left a review or comment, discovered via their events feed.

        The /users/<login>/events endpoint is the authoritative source for a
        user's own activity and is not subject to search-index lag. It only
        returns the last ~300 events (≈90 days for most engineers), so we
        still keep the gh-search path for older activity.

        Limits to org repos so we don't pull noise from external contributions.
        """
        try:
            result = subprocess.run(
                ['gh', 'api', f'/users/{username}/events', '--paginate'],
                capture_output=True, text=True, check=True, timeout=60,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.debug(f"events feed unavailable for {username}: {e}")
            return []

        out = []
        seen = set()
        # --paginate concatenates pages of JSON arrays; parse them all.
        for chunk in result.stdout.split('\n'):
            chunk = chunk.strip()
            if not chunk or not chunk.startswith('['):
                continue
            try:
                events = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            for ev in events:
                ev_type = ev.get('type')
                if ev_type not in ('PullRequestReviewEvent',
                                   'PullRequestReviewCommentEvent',
                                   'IssueCommentEvent'):
                    continue
                created_at = ev.get('created_at')
                if not created_at:
                    continue
                try:
                    ts = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                except ValueError:
                    continue
                if ts.replace(tzinfo=None) < cutoff:
                    continue

                repo = (ev.get('repo') or {}).get('name') or ''
                if not repo.startswith(f"{self.organization}/"):
                    continue

                payload = ev.get('payload') or {}
                pr = payload.get('pull_request') or {}
                number = pr.get('number')
                url = pr.get('html_url')

                # IssueCommentEvent fires on both issues and PRs; filter to PRs.
                if ev_type == 'IssueCommentEvent':
                    issue = payload.get('issue') or {}
                    if not issue.get('pull_request'):
                        continue
                    number = number or issue.get('number')
                    url = url or issue.get('html_url')

                if not number:
                    continue
                key = (repo, number)
                if key in seen:
                    continue
                seen.add(key)
                out.append((repo, number, url or f"https://github.com/{repo}/pull/{number}"))
        return out

    def _fetch_pr_reviews(self, repo: str, number: int) -> List[Dict[str, Any]]:
        try:
            result = subprocess.run(
                ['gh', 'api', f'repos/{repo}/pulls/{number}/reviews', '--paginate'],
                capture_output=True, text=True, check=True, timeout=30,
            )
            payload = json.loads(result.stdout) if result.stdout.strip() else []
            # --paginate can concatenate multiple JSON arrays; handle both shapes
            if isinstance(payload, list):
                return payload
            return []
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            logger.debug(f"Could not fetch reviews for {repo}#{number}: {e}")
            return []

    def _count_inline_comments_per_review(self, repo: str, number: int) -> Dict[int, int]:
        """Count inline diff comments grouped by their parent pull_request_review_id."""
        try:
            result = subprocess.run(
                ['gh', 'api', f'repos/{repo}/pulls/{number}/comments', '--paginate'],
                capture_output=True, text=True, check=True, timeout=30,
            )
            payload = json.loads(result.stdout) if result.stdout.strip() else []
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            logger.debug(f"Could not fetch inline comments for {repo}#{number}: {e}")
            return {}
        counts: Dict[int, int] = {}
        if isinstance(payload, list):
            for c in payload:
                rid = c.get('pull_request_review_id')
                if rid:
                    counts[rid] = counts.get(rid, 0) + 1
        return counts

    def _fetch_pr_issue_comments(self, repo: str, number: int) -> List[Dict[str, Any]]:
        try:
            result = subprocess.run(
                ['gh', 'api', f'repos/{repo}/issues/{number}/comments', '--paginate'],
                capture_output=True, text=True, check=True, timeout=30,
            )
            payload = json.loads(result.stdout) if result.stdout.strip() else []
            return payload if isinstance(payload, list) else []
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            logger.debug(f"Could not fetch PR comments for {repo}#{number}: {e}")
            return []

