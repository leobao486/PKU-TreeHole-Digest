from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .config import DeepSeekConfig
from .posts import Post


@dataclass(slots=True)
class ClassificationResult:
    posts: list[Post]
    overview: str


class DeepSeekClassifier:
    def __init__(self, api_key: str, config: DeepSeekConfig):
        if not api_key.strip():
            raise RuntimeError("尚未保存 DeepSeek API Key。请在桌面程序中填写并保存。")
        self.api_key = api_key.strip()
        self.config = config

    def verify(self) -> None:
        response = requests.get(
            self.config.base_url.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=30,
        )
        response.raise_for_status()
        ids = {item.get("id") for item in response.json().get("data", [])}
        if self.config.model not in ids:
            raise RuntimeError(f"DeepSeek 账户当前不可用模型中没有 {self.config.model}。")

    def _classify_batch(self, posts: list[Post], profile: str, site_context: str) -> dict[str, Any]:
        post_payload = [
            {
                "pid": post.pid,
                "text": post.text[:1800],
                "comments": post.comments[:5],
                "created_at": post.created_at.isoformat() if post.created_at else None,
                "likes": post.likes,
                "favorites": post.favorites,
                "replies": post.replies,
                "forced_hot_topic": post.is_hot_topic,
                "is_update": post.is_update,
                "update_note": post.update_note,
            }
            for post in posts
        ]
        system_prompt = f"""你是北京大学学生的私人树洞信息助理。你的任务不是概括所有帖子，而是依据个人画像判断每条帖子对该用户的实际价值。

个人画像与可修改分类依据：
{profile}

对树洞页面、字段和社区表达方式的观察：
{site_context}

规则：
1. 逐条判断，不要因为帖子热门就默认有价值。
2. 截止日期、选课/考试、科研机会、实习招聘、奖助信息、校园服务变动、可执行的资源互助应提高分数。
3. 纯情绪宣泄、重复争论、无事实依据的八卦、广告引流通常降低分数，除非个人画像明确关注。
4. 不推断帖子没有提供的事实；保留洞号作为依据。
5. relevance 为 0-100，必须拉开有价值内容与普通内容的分差；程序会结合当天总量自适应选择。
6. category 使用个人画像中的类别；不合适时用“其他”。
7. 为每条帖子生成 summary，用 1-2 句客观概括与用户最相关的事实；不要生成紧急度、入选原因、热度依据或建议行动。
8. 学业、科研、升学、职业、校园服务、活动和资源互助内容应结合个人画像判断实际价值。
9. 转让、名额、票务或拼场类帖子只在资源仍有效时提高分数；正文或评论出现“已出、已转、已齐、已满、满员、没有空位”等完成状态时 relevance 必须为 0。
10. 不要为了凑数虚高评分，也不要因为当天高价值帖子多就压低评分；程序会以扫描总数约 10% 为中心，根据你给出的相关度分布在一定范围内增减最终数量。
11. 各类步骤具体、可实践的技能、小技巧、效率方法、软件工具、学习方法和生活经验可以入选。
12. forced_hot_topic 为 true 的帖子会在报告高热度分区强制展示；仍需返回它的准确分类、相关度和摘要，不要省略。
13. overview 是当前批次的整体概览，简洁说明最值得关注的主题；按照个人画像中的优先级组织内容，避免逐条罗列。
14. 必须输出 JSON 对象，格式为：
{{"overview":"批次整体概览","items":[{{"pid":"洞号","relevance":80,"category":"类别","summary":"事实摘要"}}]}}
"""
        response = requests.post(
            self.config.base_url.rstrip("/") + "/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(post_payload, ensure_ascii=False)},
                ],
                "thinking": {"type": self.config.thinking},
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
                "max_tokens": 8000,
                "stream": False,
            },
            timeout=180,
        )
        response.raise_for_status()
        payload = response.json()
        try:
            content = payload["choices"][0]["message"]["content"]
            result = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("DeepSeek 返回了无法解析的分类结果。") from exc
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise RuntimeError("DeepSeek 分类结果缺少 items 数组。")
        return result

    def _merge_overviews(self, overviews: list[str]) -> str:
        clean = [item.strip() for item in overviews if item.strip()]
        if not clean:
            return ""
        if len(clean) == 1:
            return clean[0]
        try:
            response = requests.post(
                self.config.base_url.rstrip("/") + "/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.config.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "把多个树洞批次概览合并成一段报告总览。删除重复主题和“本批次”措辞，"
                                "只写一个自然段、2至3句话；按照个人画像中的优先级组织主题，"
                                "再简要提及其他重要内容。不得生成建议行动、入选原因、"
                                "热度依据或紧急度。输出 JSON：{\"overview\":\"...\"}。"
                            ),
                        },
                        {"role": "user", "content": json.dumps(clean, ensure_ascii=False)},
                    ],
                    "thinking": {"type": self.config.thinking},
                    "response_format": {"type": "json_object"},
                    "temperature": 0.1,
                    "max_tokens": 600,
                    "stream": False,
                },
                timeout=90,
            )
            response.raise_for_status()
            payload = response.json()
            merged = json.loads(payload["choices"][0]["message"]["content"])
            overview = str(merged.get("overview") or "").strip()
            return overview or clean[0]
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            # A failed optional merge must not discard otherwise complete classification results.
            return clean[0]

    def classify(self, posts: list[Post], profile_path: Path, context_path: Path) -> ClassificationResult:
        profile = profile_path.read_text(encoding="utf-8")
        site_context = context_path.read_text(encoding="utf-8") if context_path.exists() else "暂无补充观察。"
        limited = posts
        by_pid = {post.pid: post for post in limited}
        selected: list[Post] = []
        overviews: list[str] = []
        size = max(1, self.config.batch_size)
        for start in range(0, len(limited), size):
            result = self._classify_batch(limited[start : start + size], profile, site_context)
            if result.get("overview"):
                overviews.append(str(result["overview"]).strip())
            for item in result["items"]:
                if not isinstance(item, dict):
                    continue
                post = by_pid.get(str(item.get("pid", "")))
                if post is None:
                    continue
                try:
                    relevance = max(0, min(100, int(item.get("relevance", 0))))
                except (TypeError, ValueError):
                    continue
                if relevance < self.config.fallback_relevance and not post.is_hot_topic:
                    continue
                post.score = relevance / 10
                post.category = str(item.get("category") or "其他")
                post.ai_summary = str(item.get("summary") or "").strip()
                selected.append(post)
        unique = {post.pid: post for post in selected}
        ordered = sorted(unique.values(), key=lambda post: post.score, reverse=True)
        return ClassificationResult(posts=ordered, overview=self._merge_overviews(overviews))
