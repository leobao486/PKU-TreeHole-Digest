from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import FilterConfig


@dataclass(slots=True)
class CommentDetail:
    text: str
    author: str = "匿名"
    quoted_author: str = ""
    quoted_text: str = ""


@dataclass(slots=True)
class Post:
    pid: str
    text: str
    created_at: datetime | None
    likes: int = 0
    favorites: int = 0
    replies: int = 0
    unique_repliers: int = 0
    comments: list[str] = field(default_factory=list)
    comment_details: list[CommentDetail] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    score: float = 0.0
    category: str = "其他热门"
    matched_keywords: list[str] = field(default_factory=list)
    ai_summary: str = ""
    is_update: bool = False
    update_note: str = ""
    is_hot_topic: bool = False
    heat_reason: str = ""

    @property
    def url(self) -> str:
        return f"https://treehole.pku.edu.cn/ch/web/pc/index?pid={self.pid}"


def _first(data: dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return default


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc).astimezone()
        except (OSError, OverflowError, ValueError):
            return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def _comment_items(data: dict[str, Any]) -> list[Any]:
    candidate = _first(data, ("comments", "comment_list", "replies", "reply_list", "list"), [])
    if isinstance(candidate, dict):
        candidate = _first(candidate, ("data", "list", "items"), [])
    return candidate if isinstance(candidate, list) else []


def _comment_texts(data: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for item in _comment_items(data):
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            text = _first(item, ("text", "content", "comment", "reply_content"), "")
            if text:
                result.append(str(text))
    return result


def _comment_details(data: dict[str, Any]) -> list[CommentDetail]:
    result: list[CommentDetail] = []
    for item in _comment_items(data):
        if isinstance(item, str):
            text = item.strip()
            if text:
                result.append(CommentDetail(text=text))
            continue
        if not isinstance(item, dict):
            continue
        text = str(_first(item, ("text", "content", "comment", "reply_content"), "")).strip()
        if not text:
            continue
        author = str(
            _first(item, ("name_tag", "tag", "nickname", "user_name"), "")
            or ("洞主" if item.get("is_lz") or item.get("is_author") else "匿名")
        )
        quote = item.get("quote")
        quoted_author = ""
        quoted_text = ""
        if isinstance(quote, dict):
            quoted_author = str(
                _first(quote, ("name_tag", "tag", "nickname", "user_name"), "") or "匿名"
            )
            quoted_text = str(
                _first(quote, ("text", "content", "comment", "reply_content"), "")
            ).strip()
        result.append(
            CommentDetail(
                text=text,
                author=author,
                quoted_author=quoted_author,
                quoted_text=quoted_text,
            )
        )
    return result


def _unique_comment_authors(data: dict[str, Any]) -> int:
    authors: set[str] = set()
    for item in _comment_items(data):
        if not isinstance(item, dict):
            continue
        identity = _first(
            item,
            ("name_tag", "user_id", "uid", "nickname", "user_name", "exclusive_id_id"),
        )
        if identity not in (None, "", 0):
            authors.add(str(identity))
    return len(authors)


def normalize_post(data: dict[str, Any]) -> Post | None:
    nested = data.get("hole") if isinstance(data.get("hole"), dict) else data
    pid = _first(nested, ("pid", "id", "hole_id", "post_id"))
    text = _first(nested, ("text", "content", "hole_content", "content_text", "body"), "")
    if pid is None or not str(text).strip():
        return None
    created = _first(nested, ("timestamp", "created_at", "create_time", "createdAt", "time"))
    # The current Treehole frontend labels `likenum` with the follow/bookmark
    # icon and `praise_num_show` with the thumbs-up icon.  `attention_info` is
    # only the current user's bookmark state, not the aggregate favorite count.
    likes = _first(nested, ("praise_num_show", "praise_num", "likes", "like_count"), 0)
    favorites = _first(
        nested,
        (
            "likenum",
            "collect_num",
            "collection_num",
            "favorite_num",
            "bookmark_num",
            "attention_num",
            "follow_num",
        ),
        0,
    )
    replies = _first(nested, ("reply", "reply_num", "comment_count", "comments_count"), 0)
    comments = _comment_texts(data)
    comment_details = _comment_details(data)
    unique_repliers = _unique_comment_authors(data)
    if nested is not data and not comments:
        comments = _comment_texts(nested)
        comment_details = _comment_details(nested)
        unique_repliers = _unique_comment_authors(nested)
    return Post(
        pid=str(pid),
        text=re.sub(r"\s+", " ", str(text)).strip(),
        created_at=_datetime(created),
        likes=_integer(likes),
        favorites=_integer(favorites),
        replies=_integer(replies),
        unique_repliers=unique_repliers,
        comments=comments,
        comment_details=comment_details,
        raw=data,
    )


def score_post(post: Post, config: FilterConfig, now: datetime | None = None) -> Post | None:
    searchable = (post.text + " " + " ".join(post.comments[:3])).lower()
    if any(word.lower() in searchable for word in config.blocked_keywords):
        return None
    best_category = "其他热门"
    best_category_score = 0.0
    matched: list[str] = []
    for name, category in config.categories.items():
        category_matches = [word for word in category.keywords if word.lower() in searchable]
        category_score = min(3, len(category_matches)) * category.weight
        if category_score > best_category_score:
            best_category = name
            best_category_score = category_score
            matched = category_matches
    engagement = 0.45 * math.log1p(post.likes) + 0.65 * math.log1p(post.replies)
    recency = 0.0
    if post.created_at:
        reference = now or datetime.now().astimezone()
        hours = max(0.0, (reference - post.created_at).total_seconds() / 3600)
        recency = max(0.0, 1.5 - hours / 24)
    post.score = round(best_category_score + engagement + recency, 3)
    post.category = best_category
    post.matched_keywords = matched
    if best_category == "其他热门" and not config.include_unmatched_hot_posts:
        return None
    return post if post.score >= config.min_score else None


def is_closed_offer(post: Post) -> bool:
    """Exclude time-sensitive offers whose item, slot, or quota is no longer available."""
    searchable = " ".join([post.text, *post.comments, post.ai_summary]).lower()
    is_offer = post.category in {"资源互助", "活动与兴趣"} or any(
        marker in searchable for marker in ("转让", "出一个", "空位", "拼场", "余票")
    )
    if not is_offer:
        return False
    closed_patterns = (
        r"已出(?:[\s，,。.!！；;]|$)",
        r"已经出(?:了|掉)?",
        r"已转(?:让|出|掉)?",
        r"转掉了?",
        r"已齐",
        r"人齐了?",
        r"已满(?:员)?",
        r"满员",
        r"已约满",
        r"没有空位",
        r"没位置",
        r"已售",
    )
    return any(re.search(pattern, searchable, flags=re.IGNORECASE) for pattern in closed_patterns)


def is_forced_hot_topic(post: Post) -> bool:
    reasons: list[str] = []
    if post.replies >= 20:
        reasons.append(f"评论={post.replies}≥20")
    if post.favorites >= 20:
        reasons.append(f"收藏={post.favorites}≥20")
    if not reasons:
        return False
    post.is_hot_topic = True
    post.heat_reason = "；".join(reasons)
    return True
