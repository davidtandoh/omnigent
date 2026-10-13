"""UTC calendar windows shared by cost policies and their builder."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def cost_window(period: str, today: str | None = None) -> tuple[str, str]:
    """Return the inclusive start and current day as ISO dates."""
    day = date.fromisoformat(today) if today is not None else datetime.now(timezone.utc).date()
    if period == "day":
        start = day
    elif period == "week":
        start = day - timedelta(days=day.weekday())
    elif period == "month":
        start = day.replace(day=1)
    elif period == "quarter":
        start = day.replace(month=((day.month - 1) // 3) * 3 + 1, day=1)
    elif period == "year":
        start = day.replace(month=1, day=1)
    else:
        raise ValueError(f"Invalid cost period: {period!r}")
    return start.isoformat(), day.isoformat()
