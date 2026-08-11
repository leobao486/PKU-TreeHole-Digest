from __future__ import annotations

import json
import os
import secrets
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .posts import CommentDetail, Post, normalize_post


FAVORITES_API_PORT = 8765
_LOCK = threading.RLock()


def favorites_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    return base / "PKUTreeholeDigest" / "favorites.json"


def favorites_token_path() -> Path:
    return favorites_path().with_name("favorites_api_token.txt")


def favorites_api_token() -> str:
    path = favorites_token_path()
    with _LOCK:
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token
        path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        path.write_text(token, encoding="utf-8")
        return token


def _default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "folders": [{"id": "default", "name": "默认收藏夹"}],
        "items": {},
    }


def _normalize_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return _default_state()
    folders = [
        {"id": str(item.get("id", "")).strip(), "name": str(item.get("name", "")).strip()}
        for item in value.get("folders", [])
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    ]
    if not folders:
        folders = [{"id": "default", "name": "默认收藏夹"}]
    folder_ids = {item["id"] for item in folders}
    default_id = folders[0]["id"]
    items: dict[str, dict[str, Any]] = {}
    raw_items = value.get("items", {})
    if isinstance(raw_items, dict):
        for pid, item in raw_items.items():
            if not isinstance(item, dict):
                continue
            clean = dict(item)
            clean["pid"] = str(pid)
            if str(clean.get("folder_id", "")) not in folder_ids:
                clean["folder_id"] = default_id
            clean["post"] = _sanitize_post_payload(clean.get("post", {}), str(pid))
            try:
                clean["last_seen_replies"] = max(0, int(clean.get("last_seen_replies", 0)))
            except (TypeError, ValueError):
                clean["last_seen_replies"] = 0
            items[str(pid)] = clean
    return {"version": 1, "folders": folders, "items": items}


def load_favorites() -> dict[str, Any]:
    path = favorites_path()
    with _LOCK:
        if not path.exists():
            return _default_state()
        try:
            return _normalize_state(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return _default_state()


def save_favorites(state: dict[str, Any]) -> None:
    clean = _normalize_state(state)
    path = favorites_path()
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


def _sanitize_comment(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    text = str(value.get("text", "")).strip()
    if not text:
        return None
    return {
        "text": text,
        "author": str(value.get("author", "匿名") or "匿名")[:80],
        "quoted_author": str(value.get("quoted_author", ""))[:80],
        "quoted_text": str(value.get("quoted_text", "")).strip(),
    }


def _sanitize_post_payload(value: Any, pid: str = "") -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    comments = [
        clean
        for item in source.get("comments", []) if (clean := _sanitize_comment(item)) is not None
    ] if isinstance(source.get("comments", []), list) else []

    def number(name: str) -> int:
        try:
            return max(0, int(source.get(name, 0)))
        except (TypeError, ValueError):
            return 0

    return {
        "pid": str(source.get("pid") or pid),
        "text": str(source.get("text", "")),
        "created_at": str(source.get("created_at", "")),
        "summary": str(source.get("summary", "")),
        "category": str(source.get("category", "其他")),
        "replies": number("replies"),
        "favorites": number("favorites"),
        "likes": number("likes"),
        "comments": comments,
    }


def post_to_favorite_payload(post: Post) -> dict[str, Any]:
    details = post.comment_details or [CommentDetail(text=text) for text in post.comments]
    return {
        "pid": post.pid,
        "text": post.text,
        "created_at": post.created_at.isoformat() if post.created_at else "",
        "summary": post.ai_summary,
        "category": post.category,
        "replies": post.replies,
        "favorites": post.favorites,
        "likes": post.likes,
        "comments": [
            {
                "text": comment.text,
                "author": comment.author,
                "quoted_author": comment.quoted_author,
                "quoted_text": comment.quoted_text,
            }
            for comment in details
        ],
    }


def favorites_snapshot(state: dict[str, Any] | None = None) -> dict[str, Any]:
    current = _normalize_state(state if state is not None else load_favorites())
    items: list[dict[str, Any]] = []
    for item in current["items"].values():
        post = item["post"]
        current_replies = int(post.get("replies", 0))
        seen = int(item.get("last_seen_replies", 0))
        items.append(
            {
                **item,
                "unread": current_replies > seen,
                "new_replies": max(0, current_replies - seen),
            }
        )
    items.sort(key=lambda item: str(item.get("added_at", "")), reverse=True)
    return {"version": 1, "folders": current["folders"], "items": items}


def add_favorite(pid: str, folder_id: str, post: Any) -> dict[str, Any]:
    with _LOCK:
        state = load_favorites()
        folder_ids = {folder["id"] for folder in state["folders"]}
        chosen = folder_id if folder_id in folder_ids else state["folders"][0]["id"]
        clean_post = _sanitize_post_payload(post, pid)
        existing = state["items"].get(pid)
        if existing:
            if not clean_post.get("summary"):
                clean_post["summary"] = str(existing.get("post", {}).get("summary", ""))
            existing["folder_id"] = chosen
            existing["post"] = clean_post
        else:
            state["items"][pid] = {
                "pid": pid,
                "folder_id": chosen,
                "post": clean_post,
                "last_seen_replies": int(clean_post.get("replies", 0)),
                "added_at": datetime.now().astimezone().isoformat(),
            }
        save_favorites(state)
        return favorites_snapshot(state)


def remove_favorite(pid: str) -> dict[str, Any]:
    with _LOCK:
        state = load_favorites()
        state["items"].pop(pid, None)
        save_favorites(state)
        return favorites_snapshot(state)


def create_folder(name: str) -> dict[str, Any]:
    clean_name = name.strip()[:60]
    if not clean_name:
        raise ValueError("收藏夹名称不能为空。")
    with _LOCK:
        state = load_favorites()
        state["folders"].append({"id": uuid.uuid4().hex, "name": clean_name})
        save_favorites(state)
        return favorites_snapshot(state)


def rename_folder(folder_id: str, name: str) -> dict[str, Any]:
    clean_name = name.strip()[:60]
    if not clean_name:
        raise ValueError("收藏夹名称不能为空。")
    with _LOCK:
        state = load_favorites()
        folder = next((item for item in state["folders"] if item["id"] == folder_id), None)
        if folder is None:
            raise ValueError("找不到该收藏夹。")
        folder["name"] = clean_name
        save_favorites(state)
        return favorites_snapshot(state)


def move_favorite(pid: str, folder_id: str) -> dict[str, Any]:
    with _LOCK:
        state = load_favorites()
        if folder_id not in {folder["id"] for folder in state["folders"]}:
            raise ValueError("找不到目标收藏夹。")
        item = state["items"].get(pid)
        if item is None:
            raise ValueError("找不到该收藏帖子。")
        item["folder_id"] = folder_id
        save_favorites(state)
        return favorites_snapshot(state)


def mark_favorite_read(pid: str) -> dict[str, Any]:
    with _LOCK:
        state = load_favorites()
        item = state["items"].get(pid)
        if item is not None:
            item["last_seen_replies"] = int(item["post"].get("replies", 0))
            save_favorites(state)
        return favorites_snapshot(state)


def refresh_favorites(client: Any, comment_page_size: int = 50) -> dict[str, Any]:
    """Refresh every saved hole directly by pid without invoking DeepSeek."""
    with _LOCK:
        state = load_favorites()
        changed = False
        for pid, item in state["items"].items():
            try:
                detail = client.post_detail(pid)
                post = normalize_post(detail) if detail else None
                if post is None:
                    continue
                client.enrich_post(post, comment_page_size)
            except RuntimeError:
                continue
            saved = item.get("post", {})
            post.ai_summary = str(saved.get("summary", ""))
            post.category = str(saved.get("category", "其他"))
            item["post"] = post_to_favorite_payload(post)
            item["last_checked_at"] = datetime.now().astimezone().isoformat()
            changed = True
        if changed:
            save_favorites(state)
        return favorites_snapshot(state)


class _FavoritesHandler(BaseHTTPRequestHandler):
    server_version = "PKUTreeholeFavorites/1.0"

    def _headers(self, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-PKU-Digest-Token")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _authorized(self) -> bool:
        return secrets.compare_digest(
            self.headers.get("X-PKU-Digest-Token", ""), favorites_api_token()
        )

    def _json_body(self) -> dict[str, Any]:
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 20_000_000)
            value = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("请求内容不是有效 JSON。")
        return value if isinstance(value, dict) else {}

    def _write(self, value: Any, status: int = 200) -> None:
        self._headers(status)
        self.wfile.write(json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._headers(204)

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._write({"error": "unauthorized"}, 403)
            return
        if self.path == "/api/favorites":
            self._write(favorites_snapshot())
        else:
            self._write({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._write({"error": "unauthorized"}, 403)
            return
        try:
            body = self._json_body()
            pid = str(body.get("pid", "")).strip()
            if self.path == "/api/favorites/add":
                if not pid:
                    raise ValueError("缺少洞号。")
                result = add_favorite(pid, str(body.get("folder_id", "")), body.get("post", {}))
            elif self.path == "/api/favorites/remove":
                result = remove_favorite(pid)
            elif self.path == "/api/folders/create":
                result = create_folder(str(body.get("name", "")))
            elif self.path == "/api/folders/rename":
                result = rename_folder(str(body.get("folder_id", "")), str(body.get("name", "")))
            elif self.path == "/api/favorites/move":
                result = move_favorite(pid, str(body.get("folder_id", "")))
            elif self.path == "/api/favorites/read":
                result = mark_favorite_read(pid)
            else:
                self._write({"error": "not found"}, 404)
                return
            self._write(result)
        except ValueError as exc:
            self._write({"error": str(exc)}, 400)
        except Exception:
            self._write({"error": "本地收藏服务处理失败。"}, 500)

    def log_message(self, format: str, *args: Any) -> None:
        return


class FavoritesServer:
    def __init__(self, port: int = FAVORITES_API_PORT):
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if self._server is not None:
            return True
        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", self.port), _FavoritesHandler)
        except OSError:
            return False
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            self._thread = None
