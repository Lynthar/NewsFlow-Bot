"""Small time-formatting helpers used by user-facing command output.

Intentionally minimal — no i18n, no locale. All output is English, short,
and suitable for embedding in one-line status chips like "2h ago" or "in 15m".
"""

from datetime import datetime, timezone


def _ensure_utc(dt: datetime) -> datetime:
    """Naive datetimes are treated as UTC. SQLAlchemy can hand back either."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def relative_time(dt: datetime | None) -> str:
    """Format a past datetime as 'just now' / 'Xm ago' / 'Xh ago' / 'Xd ago'.

    Returns 'never' for None. Future times collapse to 'just now' (handles
    minor clock skew without leaking oddness to users).
    """
    if dt is None:
        return "never"
    delta = (datetime.now(timezone.utc) - _ensure_utc(dt)).total_seconds()
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def time_until(dt: datetime | None) -> str:
    """Format a future datetime as 'now' / 'in Xm' / 'in Xh' / 'in Xd'.

    Returns 'never' for None, 'now' for times already passed.
    """
    if dt is None:
        return "never"
    delta = (_ensure_utc(dt) - datetime.now(timezone.utc)).total_seconds()
    if delta <= 0:
        return "now"
    if delta < 60:
        return "soon"
    if delta < 3600:
        return f"in {int(delta // 60)}m"
    if delta < 86400:
        return f"in {int(delta // 3600)}h"
    return f"in {int(delta // 86400)}d"
