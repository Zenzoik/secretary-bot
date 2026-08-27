from __future__ import annotations

from enum import StrEnum


class LogAction(StrEnum):
    """Every terminal outcome of the pipeline, as stored in ``message_log.action``."""

    REPLIED = "replied"
    DRY_RUN = "dry_run"
    SKIPPED_UNSUPPORTED_CONTENT = "skipped_unsupported_content"
    SKIPPED_INACTIVE = "skipped_inactive"
    SKIPPED_KILL_SWITCH = "skipped_kill_switch"
    SKIPPED_EXCLUDED = "skipped_excluded"
    SKIPPED_SCHEDULE = "skipped_schedule"
    SKIPPED_WINDOW_LIMIT = "skipped_window_limit"
    SKIPPED_OWNER_REPLIED = "skipped_owner_replied"
    ERROR = "error"


ACTION_SQL_LIST = ", ".join(f"'{action.value}'" for action in LogAction)
