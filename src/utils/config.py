"""
Configuration file loading and validation.
"""

import yaml
import os
from pathlib import Path
from typing import Dict, Any
import logging

logger = logging.getLogger(__name__)


def load_config(config_path: str = None) -> Dict[str, Any]:
    """
    Load and validate configuration from YAML file.

    Args:
        config_path: Path to config file. If None, uses default location.

    Returns:
        Dict: Configuration dictionary

    Raises:
        FileNotFoundError: If config file doesn't exist
        ValueError: If config validation fails
    """
    if config_path is None:
        # Default to config/team_config.yaml in project root
        project_root = Path(__file__).parent.parent.parent
        config_path = project_root / "config" / "team_config.yaml"

    config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    logger.info(f"Loading configuration from {config_path}")

    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    # Validate required fields
    _validate_config(config)

    # Environment variable substitution
    config = _substitute_env_vars(config)

    return config


def _validate_config(config: Dict[str, Any]) -> None:
    """
    Validate configuration structure and required fields.

    Args:
        config: Configuration dictionary

    Raises:
        ValueError: If validation fails
    """
    required_sections = ['jira', 'github', 'database', 'logging']
    for section in required_sections:
        if section not in config:
            raise ValueError(f"Missing required config section: {section}")

    # Validate Jira config
    if 'cloud_id' not in config['jira']:
        raise ValueError("Missing jira.cloud_id in config")
    if 'sprint_prefix' not in config['jira']:
        raise ValueError("Missing jira.sprint_prefix in config")

    # Validate GitHub config
    if 'organization' not in config['github']:
        raise ValueError("Missing github.organization in config")

    # Validate database config
    if 'path' not in config['database']:
        raise ValueError("Missing database.path in config")

    # Validate logging config
    if 'file' not in config['logging']:
        raise ValueError("Missing logging.file in config")

    # Team members can be empty but should exist
    if 'team_members' not in config or config['team_members'] is None:
        logger.warning("No team_members defined in config")
        config['team_members'] = []

    team_count = len(config.get('team_members', [])) if config.get('team_members') else 0
    logger.info(f"Configuration validated successfully. Team members: {team_count}")

    _validate_team_member_levels(config)


def _validate_team_member_levels(config: Dict[str, Any]) -> None:
    """Warn when a team member's `level` isn't in competencies.TITLE_TO_LEVEL.

    Members without a level field are fine — the dashboard just doesn't show
    a competency button. But a level that's mistyped (e.g. "Senior Engineer III")
    would silently fall through to the "no competency" path and the user might
    never notice. Logging a warning here surfaces it on every load.
    """
    try:
        # Local import — competencies imports nothing from this module.
        from utils.competencies import TITLE_TO_LEVEL
    except Exception:
        return  # competencies isn't on the path (smoke-test scenario) — skip silently

    unknown = []
    for member in config.get('team_members', []) or []:
        level = member.get('level')
        if not level:
            continue
        if level not in TITLE_TO_LEVEL:
            unknown.append((member.get('name', '?'), level))
    for name, level in unknown:
        logger.warning(
            "team_config: %s has unknown level %r (not in competencies.TITLE_TO_LEVEL)",
            name, level,
        )


def _substitute_env_vars(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Substitute environment variables in config values.

    Supports ${VAR_NAME} syntax.

    Args:
        config: Configuration dictionary

    Returns:
        Dict: Configuration with substituted values
    """
    def substitute_value(value):
        if isinstance(value, str) and '${' in value:
            # Simple substitution: ${VAR_NAME}
            import re
            pattern = r'\$\{([^}]+)\}'

            def replacer(match):
                var_name = match.group(1)
                return os.environ.get(var_name, match.group(0))

            return re.sub(pattern, replacer, value)
        elif isinstance(value, dict):
            return {k: substitute_value(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [substitute_value(item) for item in value]
        else:
            return value

    return substitute_value(config)


def get_team_member_by_jira_id(config: Dict[str, Any], jira_account_id: str) -> Dict[str, str]:
    """
    Get team member info by Jira account ID.

    Args:
        config: Configuration dictionary
        jira_account_id: Jira account ID

    Returns:
        Dict with team member info, or None if not found
    """
    for member in config.get('team_members', []):
        if member.get('jira_account_id') == jira_account_id:
            return member
    return None


def get_team_member_by_github(config: Dict[str, Any], github_username: str) -> Dict[str, str]:
    """
    Get team member info by GitHub username.

    Args:
        config: Configuration dictionary
        github_username: GitHub username

    Returns:
        Dict with team member info, or None if not found
    """
    for member in config.get('team_members', []):
        if member.get('github_username') == github_username:
            return member
    return None
