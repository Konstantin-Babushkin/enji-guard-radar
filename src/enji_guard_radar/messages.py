"""Alert text for Telegram (HTML parse mode). One escaping point for dynamic text."""

import html
from collections.abc import Sequence
from datetime import UTC

from enji_guard_radar.model import SEVERITY_ORDER, Alert, AlertKind, OutboundMessage

TELEGRAM_MAX_CHARS = 4096


def escape(text: str) -> str:
    return html.escape(text, quote=False)


def _days(count: int) -> str:
    return "1 day" if count == 1 else f"{count} days"


def render_alert(alert: Alert) -> str:
    name = escape(alert.project_name)
    severity = f"Severity: {alert.severity.value}."
    if alert.kind is AlertKind.STALE:
        if alert.age_days is None or alert.last_run_at is None:
            message = "a stale alert needs age_days and last_run_at"
            raise ValueError(message)
        last = alert.last_run_at.astimezone(UTC).date().isoformat()
        lines = [
            f"Stale Guard scans: <b>{name}</b>",
            f"No completed Guard run for {_days(alert.age_days)} (last: {last}).",
            severity,
        ]
    else:
        if alert.repo_count == 0:
            detail = "It has no repositories in Guard."
        elif alert.repo_count == 1:
            detail = "No completed Guard run found in its 1 repository."
        else:
            detail = f"No completed Guard run found in its {alert.repo_count} repositories."
        lines = [f"No Guard runs: <b>{name}</b>", detail, severity]
    return "\n".join(lines)


def _sort_key(alert: Alert) -> tuple[int, int, int, str, str]:
    age = alert.age_days
    return (
        SEVERITY_ORDER.index(alert.severity),
        1 if age is None else 0,
        -(age or 0),
        alert.project_name.casefold(),
        alert.key.as_text(),
    )


def render_messages(alerts: Sequence[Alert]) -> list[OutboundMessage]:
    """One message per alert, worst first."""
    return [
        OutboundMessage(text=render_alert(alert), keys=(alert.key,))
        for alert in sorted(alerts, key=_sort_key)
    ]
