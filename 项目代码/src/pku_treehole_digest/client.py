from __future__ import annotations

import time
from itertools import count
from datetime import datetime
from typing import Any, Callable, Iterator

import requests

from .auth import Credentials
from .posts import Post, normalize_post


class AuthenticationError(RuntimeError):
    pass


class TreeholeClient:
    BASE_URL = "https://treehole.pku.edu.cn/chapi"

    def __init__(self, credentials: Credentials, request_interval: float = 1.0):
        self.credentials = credentials
        self.request_interval = max(0.0, request_interval)
        self._last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Authorization": f"Bearer {credentials.token}",
                "Referer": "https://treehole.pku.edu.cn/ch/web/pc/index",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/135 Safari/537.36"
                ),
                "userAgent": "pku_web",
                "uuid": credentials.uuid,
            }
        )

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.request_interval:
            time.sleep(self.request_interval - elapsed)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        unwrap_data: bool = True,
    ) -> Any:
        self._throttle()
        try:
            response = self.session.request(
                method,
                self.BASE_URL + path,
                params=params,
                json=json,
                timeout=(10, 20),
            )
        except requests.Timeout as exc:
            raise RuntimeError(
                f"树洞接口响应超时：{path}。请检查网络后重试，不要连续点击按钮。"
            ) from exc
        except requests.ConnectionError as exc:
            raise RuntimeError(f"无法连接树洞接口：{path}。请检查网络后重试。") from exc
        self._last_request = time.monotonic()
        if response.status_code == 401:
            raise AuthenticationError("树洞登录已过期，请在桌面程序中重新登录。")
        if response.status_code == 405:
            raise RuntimeError(
                f"树洞接口拒绝了 {method.upper()} 请求（HTTP 405）：{path}。"
                "网站接口可能刚刚更新，请保留此提示以便排查。"
            )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("树洞接口没有返回 JSON；登录可能已失效或接口已改版。") from exc
        if isinstance(payload, dict):
            code = payload.get("code")
            if code in (40002, 40008, 40088):
                raise AuthenticationError(f"树洞要求额外验证：{payload.get('message', code)}")
            if code not in (None, 20000) and not payload.get("success", False):
                raise RuntimeError(f"树洞接口错误：{payload.get('message', code)}")
            return payload.get("data", payload) if unwrap_data else payload
        return payload

    def _get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, data: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, json=data, unwrap_data=False)

    @staticmethod
    def _items(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("data", "list", "items", "records", "rows"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    def verify(self) -> dict[str, Any]:
        # `/api/v3/users/info` is not stable and currently rejects GET requests.
        # Reading a minimal feed page validates the exact permission this app needs.
        items = self.feed_page(page=1, limit=1, comment_limit=1)
        return {"feed_access": True, "sample_count": len(items)}

    def send_sms_code(self) -> None:
        # The current web client sends this request without a body.
        result = self._post("/api/jwt_send_msg")
        if not isinstance(result, dict) or result.get("success") is not True:
            message = result.get("message") if isinstance(result, dict) else None
            raise RuntimeError(f"短信验证码发送失败：{message or '树洞未返回成功状态'}")

    def verify_sms_code(self, code: str) -> None:
        code = code.strip()
        if not code:
            raise ValueError("请输入短信验证码。")
        result = self._post("/api/jwt_msg_verify", {"valid_code": code})
        if not isinstance(result, dict) or result.get("success") is not True:
            message = result.get("message") if isinstance(result, dict) else None
            raise AuthenticationError(f"短信验证失败：{message or '验证码错误或已过期'}")

    def needs_sms_verification(self) -> bool:
        """Use the feed endpoint because basic user info may work before SMS verification."""
        try:
            self.feed_page(page=1, limit=1, comment_limit=1)
        except AuthenticationError as exc:
            if "验证" in str(exc):
                return True
            raise
        return False

    def feed_page(self, page: int, limit: int, comment_limit: int) -> list[dict[str, Any]]:
        data = self._get(
            "/api/v3/hole/list_comments",
            page=page,
            limit=limit,
            comment_limit=comment_limit,
            comment_stream=1,
        )
        return self._items(data)

    def iter_feed_since(
        self,
        cutoff: datetime,
        limit: int,
        comment_limit: int,
        progress: Callable[[int, int], None] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Read every page until the chronologically ordered feed crosses cutoff."""
        seen_ids: set[str] = set()
        for page in count(1):
            items = self.feed_page(page, limit, comment_limit)
            if not items:
                break
            reached_cutoff = False
            fresh_count = 0
            for item in items:
                post = normalize_post(item)
                if post is None:
                    continue
                if post.pid in seen_ids:
                    continue
                seen_ids.add(post.pid)
                fresh_count += 1
                if post.created_at is not None and post.created_at < cutoff:
                    reached_cutoff = True
                    continue
                yield item
            if progress is not None:
                progress(page, len(seen_ids))
            if reached_cutoff or fresh_count == 0:
                break

    def post_detail(self, pid: str) -> dict[str, Any] | None:
        data = self._get("/api/v3/hole/one", pid=pid)
        if not isinstance(data, dict):
            return None
        hole = data.get("hole")
        if not isinstance(hole, dict):
            return data
        result = dict(hole)
        comments = data.get("list")
        if isinstance(comments, list):
            result["comments"] = comments
        if data.get("total") is not None:
            result["reply"] = data["total"]
        return result

    def all_comment_items(self, pid: str, page_size: int = 50) -> list[dict[str, Any]]:
        """Fetch every available comment page for a post, with no total-count cap."""
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        size = max(1, int(page_size))
        for page in count(1):
            data = self._get(
                "/api/v3/comment/list",
                pid=pid,
                page=page,
                limit=size,
                sort=0,
                comment_stream=1,
            )
            items = self._items(data)
            if not items:
                break
            fresh = 0
            for index, item in enumerate(items):
                identity = str(
                    item.get("cid")
                    or item.get("id")
                    or item.get("comment_id")
                    or f"{page}:{index}:{item.get('text') or item.get('content') or ''}"
                )
                if identity in seen:
                    continue
                seen.add(identity)
                result.append(item)
                fresh += 1
            if fresh == 0:
                break
            total = data.get("total") if isinstance(data, dict) else None
            try:
                if total is not None and len(result) >= int(total):
                    break
            except (TypeError, ValueError):
                pass
        return result

    def enrich_post(self, post: Post, comment_page_size: int = 50) -> Post:
        """Attach a selected post's complete comment history when the feed only has a preview."""
        if post.replies <= len(post.comments):
            return post
        comment_items = self.all_comment_items(post.pid, comment_page_size)
        normalized = normalize_post(
            {
                "pid": post.pid,
                "text": post.text,
                "comments": comment_items,
            }
        )
        if normalized is not None:
            post.comments = normalized.comments
            post.comment_details = normalized.comment_details
            post.unique_repliers = normalized.unique_repliers
        return post
