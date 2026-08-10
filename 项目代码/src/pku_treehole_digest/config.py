from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class FetchConfig:
    initial_lookback_hours: int = 48
    page_size: int = 25
    comment_limit: int = 20
    comment_page_size: int = 50
    request_interval_seconds: float = 1.0


@dataclass(slots=True)
class CategoryConfig:
    weight: float
    keywords: list[str]


@dataclass(slots=True)
class FilterConfig:
    min_score: float = 2.0
    include_unmatched_hot_posts: bool = True
    blocked_keywords: list[str] = field(default_factory=list)
    categories: dict[str, CategoryConfig] = field(default_factory=dict)


@dataclass(slots=True)
class DeepSeekConfig:
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    batch_size: int = 40
    minimum_relevance: int = 55
    fallback_relevance: int = 45
    high_relevance: int = 75
    selection_ratio: float = 0.10
    selection_flex_ratio: float = 0.03
    selection_min_items: int = 10
    selection_max_items: int = 300
    thinking: str = "disabled"
    prompt_context_file: str = "树洞页面观察与提示词.md"


@dataclass(slots=True)
class ProfileConfig:
    path: str = "../个人画像.yaml"


@dataclass(slots=True)
class OutputConfig:
    directory: str = "reports"
    save_raw_feed: bool = False


@dataclass(slots=True)
class AppConfig:
    fetch: FetchConfig
    filters: FilterConfig
    deepseek: DeepSeekConfig
    profile: ProfileConfig
    output: OutputConfig


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"找不到配置文件：{config_path}")
    raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    category_raw = raw.get("filters", {}).get("categories", {})
    categories = {
        name: CategoryConfig(
            weight=float(value.get("weight", 1.0)),
            keywords=[str(item) for item in value.get("keywords", [])],
        )
        for name, value in category_raw.items()
    }
    filter_raw = dict(raw.get("filters", {}))
    filter_raw["categories"] = categories
    return AppConfig(
        fetch=FetchConfig(**raw.get("fetch", {})),
        filters=FilterConfig(**filter_raw),
        deepseek=DeepSeekConfig(**raw.get("deepseek", {})),
        profile=ProfileConfig(**raw.get("profile", {})),
        output=OutputConfig(**raw.get("output", {})),
    )
