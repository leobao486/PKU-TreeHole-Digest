from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .posts import Post


REPORT_TIMESTAMP_FORMAT = "%Y-%m-%d_%H%M%S"


def load_latest_report_time(directory: Path) -> datetime | None:
    """Return the newest dated HTML report time; `latest.html` is only an alias."""
    if not directory.exists():
        return None
    timezone = datetime.now().astimezone().tzinfo
    timestamps: list[datetime] = []
    for path in directory.glob("*.html"):
        if path.name.lower() == "latest.html":
            continue
        try:
            parsed = datetime.strptime(path.stem, REPORT_TIMESTAMP_FORMAT)
        except ValueError:
            continue
        timestamps.append(parsed.replace(tzinfo=timezone))
    return max(timestamps) if timestamps else None


def state_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    return base / "PKUTreeholeDigest" / "state.json"


def load_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def load_last_successful_run(state: dict[str, Any] | None = None) -> datetime | None:
    current = state or load_state()
    # Version 1 used a fixed 300-item window, so it cannot define a valid time boundary.
    if current.get("version") != 2:
        return None
    value = current.get("last_successful_run")
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).astimezone()
    except ValueError:
        return None


def load_reported_posts(state: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    value = (state or load_state()).get("reported_posts", {})
    if not isinstance(value, dict):
        return {}
    return {str(pid): item for pid, item in value.items() if isinstance(item, dict)}


def save_run_state(
    posts: list[Post], when: datetime, previous: dict[str, dict[str, Any]] | None = None
) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    reported: dict[str, dict[str, Any]] = {}
    expiry = when - timedelta(hours=48)
    for pid, item in (previous or {}).items():
        try:
            reported_at = datetime.fromisoformat(str(item.get("reported_at"))).astimezone()
        except (TypeError, ValueError):
            continue
        if reported_at >= expiry:
            reported[str(pid)] = item
    reported.update({
        post.pid: {
            "replies": post.replies,
            "category": post.category,
            "score": post.score,
            "summary": post.ai_summary,
            "reported_at": when.isoformat(),
        }
        for post in posts
    })
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "last_successful_run": when.isoformat(),
                "reported_posts": reported,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
