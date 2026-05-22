"""
Data models for metrics.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict
from datetime import datetime


@dataclass
class Sprint:
    """Jira sprint information."""
    jira_sprint_id: int
    sprint_name: str
    state: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    goal: Optional[str] = None


@dataclass
class Ticket:
    """Jira ticket/issue information."""
    ticket_key: str
    summary: str
    status: str
    assignee_account_id: Optional[str]
    assignee_display_name: Optional[str]
    story_points: Optional[float]
    issue_type: str
    priority: str
    created_at: str
    updated_at: str
    ticket_url: str

    @property
    def story_points_value(self) -> float:
        """Get story points value, defaulting to 0 if not set."""
        return self.story_points or 0.0


@dataclass
class SprintMetrics:
    """Aggregated sprint metrics."""
    sprint_id: int
    total_story_points: float = 0.0
    completed_story_points: float = 0.0
    remaining_story_points: float = 0.0
    total_tickets: int = 0
    open_tickets: int = 0
    closed_tickets: int = 0
    in_progress_tickets: int = 0

    @property
    def completion_percent(self) -> float:
        """Calculate completion percentage."""
        if self.total_story_points == 0:
            return 0.0
        return round((self.completed_story_points / self.total_story_points) * 100, 1)


@dataclass
class DeveloperMetrics:
    """Individual developer metrics."""
    developer_id: str
    developer_name: str
    assigned_story_points: float = 0.0
    completed_story_points: float = 0.0
    remaining_story_points: float = 0.0
    tickets_by_status: Dict[str, List[str]] = field(default_factory=dict)
    tickets_in_progress: int = 0
    tickets_completed: int = 0
    tickets_todo: int = 0

    @property
    def completion_percent(self) -> float:
        """Calculate completion percentage."""
        if self.assigned_story_points == 0:
            return 0.0
        return round((self.completed_story_points / self.assigned_story_points) * 100, 1)


@dataclass
class GitHubPR:
    """GitHub pull request information."""
    pr_number: int
    repository: str
    author_github_username: str
    title: str
    state: str
    created_at: str
    updated_at: Optional[str] = None
    merged_at: Optional[str] = None
    closed_at: Optional[str] = None
    pr_url: Optional[str] = None
    lines_added: int = 0
    lines_deleted: int = 0


@dataclass
class GitHubPRMetrics:
    """GitHub PR metrics for a developer."""
    developer_github_username: str
    developer_name: str
    open_pr_count: int = 0
    pr_details: List[Dict] = field(default_factory=list)
    avg_time_to_merge_hours: Optional[float] = None
    merged_pr_count: int = 0
