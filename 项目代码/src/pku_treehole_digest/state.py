from __future__ import annotations

from datetime import datetime
from pathlib import Path


REPORT_TIMESTAMP_FORMAT = "%Y-%m-%d_%H%M%S"


def latest_report_path(directory: Path) -> Path | None:
    """Return the newest timestamped HTML report, ignoring unrelated HTML files."""
    if not directory.exists():
        return None
    candidates: list[tuple[datetime, Path]] = []
    for path in directory.glob("*.html"):
        try:
            parsed = datetime.strptime(path.stem, REPORT_TIMESTAMP_FORMAT)
        except ValueError:
            continue
        candidates.append((parsed, path))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def load_latest_report_time(directory: Path) -> datetime | None:
    """Return the timestamp encoded in the newest dated HTML report."""
    path = latest_report_path(directory)
    if path is None:
        return None
    timezone = datetime.now().astimezone().tzinfo
    return datetime.strptime(path.stem, REPORT_TIMESTAMP_FORMAT).replace(tzinfo=timezone)
