from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from requests import RequestException

from .auth import (
    Credentials,
    load_deepseek_key,
    load_login_secret,
    login_with_iaaa,
    prompt_for_token,
    session_path,
)
from .client import AuthenticationError, TreeholeClient
from .config import load_config
from .deepseek_classifier import DeepSeekClassifier
from .favorites import refresh_favorites
from .posts import (
    Post,
    is_closed_badminton_offer,
    is_forced_hot_topic,
    normalize_post,
    score_post,
)
from .report import local_summary, render_report, write_report
from .state import load_latest_report_time


def command_login(args: argparse.Namespace) -> int:
    credentials = prompt_for_token() if args.token else login_with_iaaa()
    client = TreeholeClient(credentials)
    client.verify()
    path = credentials.save()
    print(f"登录成功；会话已保存在本机：{path}")
    print("账号密码未保存。")
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    credentials = Credentials.load()
    if not credentials:
        print(f"未找到登录会话（预期位置：{session_path()}）。请先运行 `pku-digest login`。")
        return 1
    config = load_config(args.config)
    info = TreeholeClient(credentials, config.fetch.request_interval_seconds).verify()
    print("树洞帖子接口连接正常。", json.dumps(info, ensure_ascii=False))
    return 0


def _authenticated_client(config) -> TreeholeClient:
    credentials = Credentials.load()
    if credentials:
        client = TreeholeClient(credentials, config.fetch.request_interval_seconds)
        try:
            client.feed_page(page=1, limit=1, comment_limit=1)
            return client
        except AuthenticationError as exc:
            if "验证" in str(exc):
                raise
    saved = load_login_secret()
    if not saved:
        raise AuthenticationError("尚未登录，也没有保存的账号密码。请先双击运行桌面程序完成设置。")
    credentials = login_with_iaaa(*saved)
    credentials.save()
    client = TreeholeClient(credentials, config.fetch.request_interval_seconds)
    client.feed_page(page=1, limit=1, comment_limit=1)
    return client


def _adaptive_select(posts: list[Post], fetched_count: int, config) -> list[Post]:
    """Select roughly 10% while letting DeepSeek's score distribution move the boundary."""
    ordered = sorted(posts, key=lambda post: post.score, reverse=True)
    if not ordered:
        return []
    minimum = max(1, int(config.selection_min_items))
    maximum = max(minimum, int(config.selection_max_items))
    center_ratio = max(0.0, float(config.selection_ratio))
    flex_ratio = max(0.0, float(config.selection_flex_ratio))
    lower_ratio = max(0.0, center_ratio - flex_ratio)
    upper_ratio = center_ratio + flex_ratio
    scanned = max(0, fetched_count)
    base = max(minimum, round(scanned * center_ratio))
    lower = max(minimum, round(scanned * lower_ratio))
    upper = max(minimum, round(scanned * upper_ratio))
    base = min(base, maximum, len(ordered))
    lower = min(lower, maximum, len(ordered))
    upper = min(upper, maximum, len(ordered))
    boundary_relevance = ordered[base - 1].score * 10
    high_count = sum(post.score * 10 >= config.high_relevance for post in ordered)
    standard_count = sum(post.score * 10 >= config.minimum_relevance for post in ordered)
    if boundary_relevance >= config.high_relevance:
        target = min(upper, max(base, high_count))
    elif boundary_relevance < config.minimum_relevance:
        target = max(lower, min(base, standard_count))
    else:
        target = base
    return ordered[:target]


def _is_current_interval_post(post: Post, cutoff: datetime, now: datetime) -> bool:
    """Only newly created posts inside this report window may enter the hot section."""
    return (
        post.created_at is not None
        and cutoff <= post.created_at <= now
    )


def command_run(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    now = datetime.now().astimezone()
    output_dir = (config_path.parent / config.output.directory).resolve()
    last_report = load_latest_report_time(output_dir)
    cutoff = last_report or now - timedelta(hours=config.fetch.initial_lookback_hours)
    client = _authenticated_client(config)
    progress_callback = getattr(args, "progress", None)
    if progress_callback is not None:
        progress_callback(-2, 0)
    favorite_snapshot = refresh_favorites(client, config.fetch.comment_page_size)
    if progress_callback is not None:
        progress_callback(-3, len(favorite_snapshot.get("items", [])))
    raw_items = list(
        client.iter_feed_since(
            cutoff,
            config.fetch.page_size,
            config.fetch.comment_limit,
            progress=progress_callback,
        )
    )
    if progress_callback is not None:
        progress_callback(-1, len(raw_items))
    if config.output.save_raw_feed:
        raw_dir = config_path.parent / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"{now.strftime('%Y-%m-%d_%H%M%S')}.json").write_text(
            json.dumps(raw_items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    normalized = [post for item in raw_items if (post := normalize_post(item))]
    recent = [post for post in normalized if post.created_at is None or post.created_at >= cutoff]
    forced_hot = [
        post
        for post in recent
        if _is_current_interval_post(post, cutoff, now)
        and is_forced_hot_topic(post)
        and not is_closed_badminton_offer(post)
    ]
    forced_hot_ids = {post.pid for post in forced_hot}
    candidates = []
    for post in recent:
        scored = score_post(post, config.filters, now)
        if scored is not None:
            candidates.append(scored)
        elif post.pid in forced_hot_ids:
            candidates.append(post)
    api_key = load_deepseek_key()
    if not api_key:
        raise RuntimeError("尚未保存 DeepSeek API Key。请双击运行桌面程序并填写。")
    profile_path = (config_path.parent / config.profile.path).resolve()
    context_path = (config_path.parent / config.deepseek.prompt_context_file).resolve()
    if not profile_path.exists():
        raise FileNotFoundError(f"找不到个人画像：{profile_path}")
    classification = DeepSeekClassifier(api_key, config.deepseek).classify(
        candidates, profile_path, context_path
    )
    classified_by_pid = {post.pid: post for post in classification.posts}
    hot_selected = []
    for original in forced_hot:
        post = classified_by_pid.get(original.pid, original)
        post.is_hot_topic = True
        post.heat_reason = original.heat_reason
        if not post.ai_summary:
            post.ai_summary = local_summary(post.text)
        hot_selected.append(post)
    eligible = [
        post
        for post in classification.posts
        if post.pid not in forced_hot_ids and not is_closed_badminton_offer(post)
    ]
    selected = _adaptive_select(eligible, len(raw_items), config.deepseek)
    selected = hot_selected + selected
    selected = [post for post in selected if not is_closed_badminton_offer(post)]
    # The feed carries only a preview of comments.  Once selection is final,
    # fetch every comment page for each included post so the expandable report
    # contains the complete discussion without an artificial comment cap.
    for post in selected:
        try:
            client.enrich_post(post, config.fetch.comment_page_size)
        except RuntimeError:
            # Keep the feed preview if one detail request is temporarily rejected;
            # a single post should not discard an otherwise complete report.
            continue
    content = render_report(
        selected,
        len(raw_items),
        now,
        config,
        classification.overview,
        period_start=cutoff,
    )
    path = write_report(content, output_dir, now)
    print(
        f"完成：时间范围 {cutoff.strftime('%m-%d %H:%M')} 至 {now.strftime('%m-%d %H:%M')}，"
        f"扫描 {len(raw_items)} 条，新帖 {len(normalized)} 条，精选 {len(selected)} 条。"
    )
    print(path)
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="北大树洞每日筛选与摘要")
    sub = result.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="通过 IAAA 登录并仅保存树洞会话令牌")
    login.add_argument("--token", action="store_true", help="改为手动输入已有 Bearer token")
    login.set_defaults(handler=command_login)
    doctor = sub.add_parser("doctor", help="检查登录与接口连接")
    doctor.add_argument("--config", default="config.yaml")
    doctor.set_defaults(handler=command_doctor)
    run = sub.add_parser("run", help="抓取并生成今日报告")
    run.add_argument("--config", default="config.yaml")
    run.set_defaults(handler=command_run)
    return result


def main() -> None:
    args = parser().parse_args()
    try:
        raise SystemExit(args.handler(args))
    except (AuthenticationError, RuntimeError, FileNotFoundError, RequestException) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
