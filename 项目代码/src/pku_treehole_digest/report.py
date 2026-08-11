from __future__ import annotations

import html
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .config import AppConfig
from .favorites import (
    FAVORITES_API_PORT,
    favorites_api_token,
    favorites_snapshot,
    post_to_favorite_payload,
)
from .posts import CommentDetail, Post


CATEGORY_PRIORITY = {
    "重要通知": 0,
    "学业与课程": 1,
    "科研与学术": 2,
    "升学与职业": 3,
    "校园服务": 10,
    "技能与经验": 20,
    "资源互助": 21,
    "活动与兴趣": 30,
    "校园生活": 20,
    "其他": 99,
}


def category_sort_key(category: str) -> tuple[int, str]:
    return CATEGORY_PRIORITY.get(category, 80), category


def post_heading(post: Post, time_text: str) -> str:
    return f"### {post.pid} · {time_text}"


def local_summary(text: str, limit: int = 150) -> str:
    clean = re.sub(r"https?://\S+", "[链接]", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    if len(clean) <= limit:
        return clean
    cut = clean[:limit].rsplit("，", 1)[0].rsplit("。", 1)[0]
    return (cut if len(cut) >= limit // 2 else clean[:limit]) + "……"


def _escape_text(value: object) -> str:
    return html.escape(str(value), quote=True)


def _rich_text(value: object) -> str:
    escaped = html.escape(str(value), quote=False)
    return escaped.replace("\n", "<br>")


def _author_background(author: str) -> str:
    digest = hashlib.sha256(author.encode("utf-8")).digest()
    hue = int.from_bytes(digest[:2], "big") % 360
    saturation = 44 + digest[2] % 12
    lightness = 92 + digest[3] % 4
    return f"hsl({hue} {saturation}% {lightness}%)"


def _script_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def _post_card(post: Post) -> str:
    pid = _escape_text(post.pid)
    time_text = post.created_at.strftime("%m-%d %H:%M") if post.created_at else "时间未知"
    summary = post.ai_summary or local_summary(post.text)
    details = post.comment_details or [CommentDetail(text=text) for text in post.comments]
    comment_rows: list[str] = []
    for index, comment in enumerate(details, start=1):
        quote_html = ""
        if comment.quoted_text:
            quote_html = (
                '<div class="quoted-comment">'
                f'<div>回复 {_escape_text(comment.quoted_author or "匿名")}</div>'
                f'<p>{_rich_text(comment.quoted_text)}</p></div>'
            )
        comment_rows.append(
            f'<li style="background:{_author_background(comment.author)}">'
            f'<div class="comment-head"><b>{_escape_text(comment.author)}</b>'
            f'<span>#{index}</span></div>{quote_html}'
            f'<div class="comment-text">{_rich_text(comment.text)}</div></li>'
        )
    comments = "".join(comment_rows)
    if not comments:
        comments = '<p class="empty-comments">当前没有可读取的评论。</p>'
    else:
        comments = f'<ol class="comment-list">{comments}</ol>'

    badges: list[str] = []
    if post.is_hot_topic:
        badges.append('<span class="badge badge-hot">高热度强制入选</span>')
    badges_html = "".join(badges)

    return f"""
<article class="post-card" id="post-{pid}">
  <header class="post-head">
    <div class="post-identity">
      <span class="hole-number" aria-label="洞号 {pid}">{pid}</span>
      <button class="copy-button" type="button" data-pid="{pid}" onclick="copyPid(this)">复制洞号</button>
      {badges_html}
    </div>
    <time datetime="{post.created_at.isoformat() if post.created_at else ''}">{_escape_text(time_text)}</time>
  </header>
  <div class="summary-box">
    <div class="summary-label">内容概述</div>
    <div class="summary-text">{_rich_text(summary)}</div>
  </div>
  <button class="open-source-button" type="button" aria-haspopup="dialog"
          onclick="openPostModal('modal-{pid}')">查看原帖与全部评论（{len(post.comments)} 条）</button>
  <footer class="post-stats" aria-label="帖子数据">
    <span><b>评论</b> {post.replies}</span>
    <span><b>收藏</b> {post.favorites}</span>
    <span><b>点赞</b> {post.likes}</span>
    <button class="favorite-button" type="button" data-pid="{pid}"
            aria-label="收藏洞号 {pid}" title="加入收藏夹" onclick="toggleFavorite('{pid}')">☆</button>
  </footer>
</article>
<dialog class="post-modal" id="modal-{pid}" onclick="closeOnBackdrop(event)">
  <div class="modal-panel">
    <header class="modal-head">
      <div><b>{pid}</b><span>原帖与全部评论</span></div>
      <button type="button" class="modal-close" aria-label="关闭" onclick="closePostModal(this)">×</button>
    </header>
    <div class="modal-scroll">
      <section class="original-post">
        <h4>原帖</h4>
        <div>{_rich_text(post.text)}</div>
      </section>
      <section class="all-comments">
        <h4>全部评论（{len(post.comments)}）</h4>
        {comments}
      </section>
    </div>
  </div>
</dialog>"""


def render_report(
    posts: list[Post],
    fetched_count: int,
    generated_at: datetime,
    config: AppConfig,
    overview: str | None = None,
    period_start: datetime | None = None,
) -> str:
    del config  # Kept in the public signature for callers and future theme settings.
    title_date = generated_at.strftime("%Y-%m-%d")
    hot_posts = sorted(
        (post for post in posts if post.is_hot_topic),
        key=lambda item: (max(item.replies, item.favorites), item.replies + item.favorites),
        reverse=True,
    )
    regular_posts = [post for post in posts if not post.is_hot_topic]
    grouped: dict[str, list[Post]] = defaultdict(list)
    for post in regular_posts:
        grouped[post.category].append(post)
    categories = sorted(grouped, key=category_sort_key)

    toc_links = ['<a href="#overview">今日概览</a>']
    sections: list[str] = []
    if hot_posts:
        toc_links.append(f'<a href="#hot-topics">高热度话题 <span>{len(hot_posts)}</span></a>')
        cards = "".join(_post_card(post) for post in hot_posts)
        sections.append(
            f'<section class="report-section" id="hot-topics"><h2>今日高热度话题</h2>'
            f'<p class="section-note">达到热度阈值后强制入选，不占当天个性化精选基准数量。</p>{cards}</section>'
        )
    for index, category in enumerate(categories, start=1):
        section_id = f"category-{index}"
        items = sorted(
            grouped[category],
            key=lambda post: (post.score, post.created_at.timestamp() if post.created_at else 0),
            reverse=True,
        )
        toc_links.append(
            f'<a href="#{section_id}">{_escape_text(category)} <span>{len(items)}</span></a>'
        )
        sections.append(
            f'<section class="report-section" id="{section_id}"><h2>{_escape_text(category)}</h2>'
            f'{"".join(_post_card(post) for post in items)}</section>'
        )

    counts = Counter(post.category for post in regular_posts)
    category_chips = "".join(
        f'<span>{_escape_text(name)} <b>{count}</b></span>'
        for name, count in sorted(counts.items(), key=lambda item: category_sort_key(item[0]))
    )
    if not category_chips:
        category_chips = "<span>本时段暂无个性化精选</span>"
    period_text = (
        f"{period_start.strftime('%Y-%m-%d %H:%M')} 至 {generated_at.strftime('%Y-%m-%d %H:%M')}"
        if period_start
        else f"截至 {generated_at.strftime('%Y-%m-%d %H:%M')}"
    )
    if overview:
        overview_text = overview
    elif posts:
        overview_text = (
            f"本次共扫描 {fetched_count} 条帖子，最终保留 {len(posts)} 条；"
            f"其中高热度强制入选 {len(hot_posts)} 条。内容概述由本地程序整理。"
        )
    else:
        overview_text = f"本次共扫描 {fetched_count} 条帖子，本时段暂无入选内容。"
    empty_state = ""
    if not posts:
        empty_state = (
            '<section class="empty-state"><h2>本时段暂无入选帖子</h2>'
            '<p>本次仍已完成扫描；上一份非空报告不会被覆盖。</p></section>'
        )
    report_posts: dict[str, dict[str, object]] = {}
    for post in posts:
        payload = post_to_favorite_payload(post)
        payload["summary"] = post.ai_summary or local_summary(post.text)
        report_posts[post.pid] = payload
    embedded_favorites = favorites_snapshot()
    favorites_token = favorites_api_token()

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>北大树洞日报 · {_escape_text(title_date)}</title>
<style>
:root{{--page:#f4f7fa;--card:#fff;--ink:#17212b;--muted:#667585;--line:#dce6ee;--nav:#eef4f8;--blue:#215f8f;--blue-soft:#e7f2fa;--summary:#eaf5fb;--hot:#fff0e7;--hot-ink:#9a3f12;--green:#e9f6ef;--shadow:0 5px 20px rgba(31,69,92,.07)}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth;scroll-padding-top:22px}}body{{margin:0;background:var(--page);color:var(--ink);font-family:"Microsoft YaHei UI","PingFang SC","Noto Sans CJK SC",sans-serif;line-height:1.72}}
.layout{{min-height:100vh;padding-left:260px}}.sidebar{{position:fixed;inset:0 auto 0 0;width:260px;padding:30px 18px;background:var(--nav);border-right:1px solid var(--line);overflow:auto;z-index:10}}.sidebar-title{{font-size:20px;font-weight:800;color:#123f60;margin:0 8px 18px}}.sidebar-subtitle{{font-size:12px;color:var(--muted);font-weight:500;margin-top:2px}}.toc{{display:flex;flex-direction:column;gap:4px}}.toc a{{display:flex;justify-content:space-between;gap:10px;padding:9px 11px;border-radius:9px;color:#33495b;text-decoration:none;font-size:14px}}.toc a:hover,.toc a:focus-visible{{background:#dbeaf4;color:#123f60;outline:none}}.toc a span{{color:#6b7e8d}}
.main{{width:100%;max-width:1060px;margin:0 auto;padding:36px 42px 70px}}.hero{{background:linear-gradient(135deg,#164c72,#2f78a8);color:white;padding:30px 34px;border-radius:20px;box-shadow:var(--shadow)}}.hero h1{{font-size:30px;line-height:1.25;margin:0 0 10px}}.hero p{{margin:0;color:#dcecf6}}.hero-stats{{display:flex;flex-wrap:wrap;gap:10px;margin-top:20px}}.hero-stats span{{background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.18);border-radius:999px;padding:5px 12px;font-size:13px}}
.overview{{margin:22px 0 34px;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px 24px;box-shadow:var(--shadow)}}.overview h2{{margin:0 0 8px;color:#174d73;font-size:21px}}.overview p{{margin:0}}.category-chips{{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}}.category-chips span{{background:#f0f5f8;border:1px solid #dae6ed;border-radius:999px;padding:4px 10px;font-size:13px;color:#465a69}}
.report-section{{margin:34px 0;scroll-margin-top:20px}}.report-section>h2{{margin:0 0 5px;padding-left:13px;border-left:5px solid #4d91c6;color:#174d73;font-size:23px}}.section-note{{margin:0 0 16px;color:var(--muted);font-size:14px}}
.post-card{{margin:16px 0;padding:20px 22px 0;background:var(--card);border:1px solid var(--line);border-radius:17px;box-shadow:var(--shadow);overflow:hidden}}.post-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:14px}}.post-identity{{display:flex;align-items:center;flex-wrap:wrap;gap:8px}}.hole-number{{font-size:20px;font-weight:850;letter-spacing:.3px;color:#123f60}}.post-head time{{color:var(--muted);font-size:13px;white-space:nowrap;padding-top:4px}}.copy-button{{font:inherit;font-size:12px;font-weight:700;color:#225f89;background:#e7f2fa;border:1px solid #bad6e8;border-radius:7px;padding:4px 9px;cursor:pointer}}.copy-button:hover{{background:#d7ebf7}}.copy-button.copied{{color:#206039;background:var(--green);border-color:#b7dfc6}}.badge{{font-size:12px;font-weight:700;border-radius:999px;padding:3px 9px}}.badge-hot{{background:var(--hot);color:var(--hot-ink)}}
.summary-box{{background:var(--summary);border:1px solid #cfe5f1;border-radius:12px;padding:14px 16px;color:#17212b}}.summary-label{{font-size:12px;line-height:1.2;color:#2f6f98;font-weight:800;margin-bottom:6px;letter-spacing:.04em}}.summary-text{{font-weight:650}}.open-source-button{{display:block;width:100%;margin-top:14px;padding:12px 4px;color:#285f85;background:transparent;border:0;border-top:1px solid var(--line);font:inherit;font-weight:750;text-align:left;cursor:pointer}}.open-source-button:hover{{color:#123f60;background:#f8fbfd}}
.post-modal{{width:760px;max-width:calc(100vw - 32px);max-height:88vh;padding:0;border:0;border-radius:18px;background:transparent;box-shadow:0 24px 80px rgba(10,29,43,.32);overflow:visible}}.post-modal::backdrop{{background:rgba(18,31,42,.52);backdrop-filter:blur(2px)}}.modal-panel{{display:flex;flex-direction:column;max-height:88vh;background:#fff;border-radius:18px;overflow:hidden}}.modal-head{{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:16px 18px;border-bottom:1px solid var(--line);background:#f7fafc}}.modal-head>div{{display:flex;align-items:baseline;gap:10px}}.modal-head b{{font-size:19px;color:#123f60}}.modal-head span{{font-size:13px;color:var(--muted)}}.modal-close{{width:34px;height:34px;border:0;border-radius:50%;background:#e8eef2;color:#344c5d;font-size:25px;line-height:1;cursor:pointer}}.modal-close:hover{{background:#dbe5eb}}.modal-scroll{{overflow-y:auto;padding:18px;overscroll-behavior:contain}}.original-post,.all-comments{{margin:0 0 16px;padding:16px 17px;border-radius:13px;background:#f7f9fb;border:1px solid #e3e9ee}}.original-post h4,.all-comments h4{{margin:0 0 10px;color:#284f6b}}.comment-list{{list-style:none;margin:0;padding:0}}.comment-list li{{position:relative;margin:10px 0;padding:12px 14px;border:1px solid rgba(65,83,96,.09);border-radius:12px;color:#17212b}}.comment-list li.new-comment{{border:2px solid #dc5a5a;box-shadow:0 0 0 2px rgba(220,90,90,.09)}}.new-reply-label{{position:absolute;right:36px;top:9px;padding:1px 7px;border-radius:999px;background:#d93030;color:#fff;font-size:10px;font-weight:800}}.comment-head{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:6px}}.comment-head b{{color:#254d68}}.comment-head span{{color:#71808c;font-size:11px}}.comment-text{{word-break:break-word}}.quoted-comment{{margin:7px 0 10px;padding:8px 11px;border-left:3px solid #7194aa;border-radius:7px;background:rgba(255,255,255,.62);font-size:13px;color:#4a5b67}}.quoted-comment>div{{font-weight:750;color:#41677e;margin-bottom:3px}}.quoted-comment p{{margin:0}}.empty-comments{{color:var(--muted);margin:0}}
.post-stats{{display:flex;align-items:center;flex-wrap:wrap;gap:22px;margin:0 -22px;padding:10px 22px;background:#f7fafc;border-top:1px solid var(--line);color:#5a6b79;font-size:13px}}.post-stats b{{color:#304c5f;margin-right:3px}}.favorite-button{{margin-left:auto;width:36px;height:36px;border:1px solid #d7e1e8;border-radius:50%;background:#fff;color:#b17a00;font-size:25px;line-height:30px;cursor:pointer}}.favorite-button:hover{{background:#fff7d6}}.favorite-button.active{{background:#fff2b8;border-color:#e3bc42;color:#9b6800}}.empty-state{{background:#fff;border:1px solid var(--line);border-radius:16px;padding:28px;text-align:center}}.footer-note{{margin-top:44px;color:#6a7885;font-size:12px;text-align:center}}
.favorites-tab{{position:fixed;right:0;top:42%;z-index:30;display:flex;align-items:center;justify-content:center;gap:9px;min-width:58px;min-height:150px;padding:20px 14px;border:1px solid #b9cfdd;border-right:0;border-radius:17px 0 0 17px;background:#fff;color:#174d73;font:inherit;font-size:16px;font-weight:850;letter-spacing:.08em;writing-mode:vertical-rl;box-shadow:0 8px 28px rgba(31,69,92,.18);cursor:pointer}}.favorites-tab:hover{{background:#eaf4fa;transform:translateX(-2px)}}.red-dot{{display:inline-block;width:9px;height:9px;border-radius:50%;background:#df2f2f;box-shadow:0 0 0 2px #fff}}.red-dot.hidden{{display:none}}.favorites-backdrop{{position:fixed;inset:0;z-index:39;background:rgba(19,34,45,.28);opacity:0;pointer-events:none;transition:opacity .22s ease}}.favorites-backdrop.open{{opacity:1;pointer-events:auto}}.favorites-drawer{{position:fixed;top:0;right:0;z-index:40;width:390px;max-width:100vw;height:100vh;background:#f7fafc;border-left:1px solid #cedce5;box-shadow:-16px 0 44px rgba(24,54,73,.18);transform:translateX(102%);transition:transform .22s ease;display:flex;flex-direction:column}}.favorites-drawer.open{{transform:translateX(0)}}.favorites-head{{display:flex;align-items:center;justify-content:space-between;padding:18px 17px;border-bottom:1px solid var(--line);background:#fff}}.favorites-head h2{{margin:0;color:#174d73;font-size:21px}}.favorites-tools{{display:flex;gap:8px}}.favorites-tools button,.folder-actions button{{border:1px solid #cbdbe5;border-radius:8px;background:#fff;color:#285f85;padding:5px 9px;cursor:pointer}}.favorites-close{{font-size:22px!important;line-height:1}}.favorites-status{{min-height:24px;padding:7px 17px;color:#697b88;font-size:12px;background:#eef4f8}}.favorites-body{{flex:1;overflow-y:auto;padding:13px}}.favorite-folder{{margin-bottom:13px;border:1px solid #d9e4eb;border-radius:13px;background:#fff;overflow:hidden}}.folder-head{{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:11px;background:#edf4f8;cursor:pointer;user-select:none}}.folder-head:hover{{background:#e3eff6}}.folder-name{{display:flex;align-items:center;gap:7px;font-weight:800;color:#294e66}}.folder-chevron{{width:15px;color:#54758a;transition:transform .16s}}.favorite-folder.expanded .folder-chevron{{transform:rotate(90deg)}}.folder-actions{{display:flex;gap:5px}}.folder-actions button.active{{background:#dbeef8;border-color:#8dbbd5}}.favorite-list{{display:none;padding:7px}}.favorite-folder.expanded .favorite-list{{display:block}}.favorite-entry{{position:relative;margin:7px 0;padding:10px 11px;border:1px solid #e0e8ed;border-radius:10px;background:#fbfdfe}}.favorite-entry.unread{{border-color:#e6a1a1;background:#fffafa}}.favorite-entry-dot{{position:absolute;right:8px;top:8px}}.favorite-entry-main{{display:block;width:100%;padding:0 15px 6px 0;border:0;background:transparent;text-align:left;color:inherit;cursor:pointer}}.favorite-entry-main b{{color:#174d73}}.favorite-entry-summary{{display:-webkit-box;margin-top:4px;color:#536571;font-size:12px;line-height:1.5;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}.favorite-entry-meta{{display:flex;gap:10px;margin-top:6px;color:#788792;font-size:11px}}.favorite-entry-controls{{display:flex;align-items:center;gap:7px;margin-top:7px}}.favorite-entry-controls select{{min-width:0;flex:1;border:1px solid #ccd9e1;border-radius:7px;padding:4px;background:#fff}}.favorite-entry-controls button{{border:0;background:transparent;color:#a13d3d;cursor:pointer}}.favorites-empty{{padding:18px 9px;text-align:center;color:#788792;font-size:13px}}.favorites-toast{{position:fixed;right:20px;bottom:22px;z-index:70;max-width:360px;padding:10px 14px;border-radius:10px;background:#243d4d;color:#fff;box-shadow:0 8px 30px rgba(0,0,0,.2);opacity:0;pointer-events:none;transform:translateY(10px);transition:.18s}}.favorites-toast.show{{opacity:1;transform:translateY(0)}}
@media (max-width:900px){{.layout{{padding-left:0}}.sidebar{{position:static;width:auto;padding:16px 18px;border-right:0;border-bottom:1px solid var(--line)}}.sidebar-title{{margin:0 5px 8px}}.toc{{flex-direction:row;overflow-x:auto;padding-bottom:3px}}.toc a{{flex:0 0 auto;background:#f7fafc}}.main{{padding:22px 16px 55px}}.hero{{padding:24px 22px}}}}
@media (max-width:560px){{.hero h1{{font-size:25px}}.post-card{{padding:17px 15px 0}}.post-head{{display:block}}.post-head time{{display:block;margin-top:8px}}.post-stats{{margin:0 -15px;padding:11px 15px;gap:13px}}.favorites-drawer{{width:100vw}}}}
@media print{{.sidebar,.copy-button,.open-source-button,.post-modal,.favorite-button,.favorites-tab,.favorites-backdrop,.favorites-drawer,.favorites-toast{{display:none}}.layout{{padding-left:0}}.main{{margin:0;max-width:none;padding:0}}.post-card{{break-inside:avoid;box-shadow:none}}}}
</style>
</head>
<body>
<div class="layout">
  <aside class="sidebar" aria-label="报告目录">
    <div class="sidebar-title">报告目录<div class="sidebar-subtitle">点击跳转到对应板块</div></div>
    <nav class="toc">{"".join(toc_links)}</nav>
  </aside>
  <main class="main" id="top">
    <header class="hero">
      <h1>北大树洞日报 · {_escape_text(title_date)}</h1>
      <p>{_escape_text(period_text)}</p>
      <div class="hero-stats"><span>扫描 {fetched_count} 条</span><span>精选 {len(posts)} 条</span><span>高热度 {len(hot_posts)} 条</span></div>
    </header>
    <section class="overview" id="overview">
      <h2>今日概览</h2>
      <p>{_rich_text(overview_text)}</p>
      <div class="category-chips">{category_chips}</div>
    </section>
    {empty_state}
    {"".join(sections)}
    <p class="footer-note">本报告由本地脚本自动生成。树洞内容可能不准确，请以学校正式通知为准。</p>
  </main>
</div>
<button class="favorites-tab" type="button" aria-controls="favorites-drawer" onclick="toggleFavoritesDrawer()">
  <span>★ 收藏夹</span><span class="red-dot hidden" id="favorites-global-dot"></span>
</button>
<div class="favorites-backdrop" id="favorites-backdrop" onclick="toggleFavoritesDrawer(false)" aria-hidden="true"></div>
<aside class="favorites-drawer" id="favorites-drawer" aria-label="收藏夹">
  <header class="favorites-head">
    <h2>收藏夹</h2>
    <div class="favorites-tools">
      <button type="button" onclick="createFavoriteFolder()">新建</button>
      <button class="favorites-close" type="button" aria-label="收起收藏夹" onclick="toggleFavoritesDrawer(false)">×</button>
    </div>
  </header>
  <div class="favorites-status" id="favorites-status">正在连接本地收藏服务……</div>
  <div class="favorites-body" id="favorites-body"></div>
</aside>
<dialog class="post-modal" id="favorite-post-modal" onclick="closeOnBackdrop(event)">
  <div class="modal-panel">
    <header class="modal-head">
      <div><b id="favorite-modal-pid"></b><span>收藏帖与全部评论</span></div>
      <button type="button" class="modal-close" aria-label="关闭" onclick="closePostModal(this)">×</button>
    </header>
    <div class="modal-scroll" id="favorite-modal-content"></div>
  </div>
</dialog>
<div class="favorites-toast" id="favorites-toast" role="status"></div>
<script>
const FAVORITES_API='http://127.0.0.1:{FAVORITES_API_PORT}';
const FAVORITES_TOKEN={_script_json(favorites_token)};
const REPORT_POSTS={_script_json(report_posts)};
let favoriteState={_script_json(embedded_favorites)};
let activeFolderId=(favoriteState.folders&&favoriteState.folders[0])?favoriteState.folders[0].id:'default';
const expandedFavoriteFolders=new Set();
let favoritesOnline=false;
function showFavoriteToast(message){{
  const toast=document.getElementById('favorites-toast');
  toast.textContent=message;toast.classList.add('show');
  window.setTimeout(()=>toast.classList.remove('show'),2200);
}}
async function favoriteRequest(path,body=null){{
  const options={{method:body===null?'GET':'POST',headers:{{'X-PKU-Digest-Token':FAVORITES_TOKEN}}}};
  if(body!==null){{options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}}
  const response=await fetch(FAVORITES_API+path,options);
  const result=await response.json();
  if(!response.ok){{throw new Error(result.error||'本地收藏服务不可用');}}
  favoritesOnline=true;favoriteState=result;renderFavorites();return result;
}}
function toggleFavoritesDrawer(force){{
  const drawer=document.getElementById('favorites-drawer');
  const shouldOpen=typeof force==='boolean'?force:!drawer.classList.contains('open');
  if(shouldOpen){{expandedFavoriteFolders.clear();renderFavorites();}}
  drawer.classList.toggle('open',shouldOpen);
  document.getElementById('favorites-backdrop').classList.toggle('open',shouldOpen);
}}
function favoriteItem(pid){{return (favoriteState.items||[]).find(item=>String(item.pid)===String(pid));}}
function updateFavoriteStars(){{
  const saved=new Set((favoriteState.items||[]).map(item=>String(item.pid)));
  document.querySelectorAll('.favorite-button').forEach(button=>{{
    const active=saved.has(String(button.dataset.pid));
    button.classList.toggle('active',active);button.textContent=active?'★':'☆';
    button.title=active?'移出收藏夹':'加入收藏夹';
  }});
}}
function element(tag,className,text){{
  const node=document.createElement(tag);if(className)node.className=className;
  if(text!==undefined)node.textContent=text;return node;
}}
function renderFavorites(){{
  const body=document.getElementById('favorites-body');body.replaceChildren();
  const folders=favoriteState.folders||[];const items=favoriteState.items||[];
  if(!folders.some(folder=>folder.id===activeFolderId)&&folders.length)activeFolderId=folders[0].id;
  document.getElementById('favorites-global-dot').classList.toggle('hidden',!items.some(item=>item.unread));
  document.getElementById('favorites-status').textContent=favoritesOnline
    ?`已连接本地收藏服务 · ${{items.length}} 条收藏`
    :`离线预览 · ${{items.length}} 条收藏（修改收藏请保持桌面程序开启）`;
  for(const folder of folders){{
    const folderItems=items.filter(item=>item.folder_id===folder.id);
    const expanded=expandedFavoriteFolders.has(folder.id);
    const wrapper=element('section','favorite-folder'+(expanded?' expanded':''));
    const head=element('div','folder-head');head.onclick=()=>{{
      if(expandedFavoriteFolders.has(folder.id))expandedFavoriteFolders.delete(folder.id);
      else expandedFavoriteFolders.add(folder.id);renderFavorites();
    }};const name=element('div','folder-name');
    name.append(element('span','folder-chevron','▶'));
    name.append(element('span','',`${{folder.name}}（${{folderItems.length}}）`));
    if(folderItems.some(item=>item.unread))name.append(element('span','red-dot'));
    const actions=element('div','folder-actions');
    const use=element('button',folder.id===activeFolderId?'active':'',folder.id===activeFolderId?'当前':'选中');
    use.type='button';use.onclick=(event)=>{{event.stopPropagation();activeFolderId=folder.id;renderFavorites();}};
    const rename=element('button','', '改名');rename.type='button';
    rename.onclick=(event)=>{{event.stopPropagation();renameFavoriteFolder(folder.id,folder.name);}};
    actions.append(use,rename);head.append(name,actions);wrapper.append(head);
    const list=element('div','favorite-list');
    if(!folderItems.length)list.append(element('div','favorites-empty','这个收藏夹还是空的。'));
    for(const item of folderItems){{
      const card=element('div','favorite-entry'+(item.unread?' unread':''));
      if(item.unread)card.append(element('span','red-dot favorite-entry-dot'));
      const main=element('button','favorite-entry-main');main.type='button';main.onclick=()=>openFavoritePost(item.pid);
      main.append(element('b','',String(item.pid)));
      const summary=element('div','favorite-entry-summary',item.post.summary||item.post.text||'暂无概述');
      const meta=element('div','favorite-entry-meta');
      meta.append(element('span','',`评论 ${{item.post.replies||0}}`),element('span','',`收藏 ${{item.post.favorites||0}}`));
      if(item.unread)meta.append(element('span','',`新增 ${{item.new_replies}}`));
      main.append(summary,meta);card.append(main);
      const controls=element('div','favorite-entry-controls');const select=document.createElement('select');
      for(const target of folders){{const option=document.createElement('option');option.value=target.id;option.textContent=target.name;option.selected=target.id===item.folder_id;select.append(option);}}
      select.onchange=()=>moveFavorite(item.pid,select.value);
      const remove=element('button','', '移除');remove.type='button';remove.onclick=()=>removeFavorite(item.pid);
      controls.append(select,remove);card.append(controls);list.append(card);
    }}
    wrapper.append(list);body.append(wrapper);
  }}
  updateFavoriteStars();
}}
async function syncFavorites(){{
  try{{await favoriteRequest('/api/favorites');}}
  catch(error){{favoritesOnline=false;renderFavorites();}}
}}
async function toggleFavorite(pid){{
  try{{
    const current=favoriteItem(pid);
    if(current){{await favoriteRequest('/api/favorites/remove',{{pid}});showFavoriteToast('已移出收藏夹');}}
    else{{
      const post=REPORT_POSTS[pid];if(!post)throw new Error('找不到该帖子的本地数据');
      await favoriteRequest('/api/favorites/add',{{pid,folder_id:activeFolderId,post}});
      showFavoriteToast('已加入收藏夹');
    }}
  }}catch(error){{showFavoriteToast(error.message+'；请保持桌面程序开启');}}
}}
async function createFavoriteFolder(){{
  const name=window.prompt('新收藏夹名称：');if(!name)return;
  try{{const result=await favoriteRequest('/api/folders/create',{{name}});activeFolderId=result.folders[result.folders.length-1].id;renderFavorites();}}
  catch(error){{showFavoriteToast(error.message);}}
}}
async function renameFavoriteFolder(folderId,currentName){{
  const name=window.prompt('修改收藏夹名称：',currentName);if(!name||name===currentName)return;
  try{{await favoriteRequest('/api/folders/rename',{{folder_id:folderId,name}});}}
  catch(error){{showFavoriteToast(error.message);}}
}}
async function moveFavorite(pid,folderId){{
  try{{await favoriteRequest('/api/favorites/move',{{pid,folder_id:folderId}});}}
  catch(error){{showFavoriteToast(error.message);}}
}}
async function removeFavorite(pid){{
  try{{await favoriteRequest('/api/favorites/remove',{{pid}});showFavoriteToast('已移出收藏夹');}}
  catch(error){{showFavoriteToast(error.message);}}
}}
function favoriteAuthorBackground(author){{
  let hash=0;for(const char of author)hash=(hash*31+char.codePointAt(0))>>>0;
  return `hsl(${{hash%360}} ${{44+(hash%12)}}% ${{92+(hash%4)}}%)`;
}}
function openFavoritePost(pid){{
  const item=favoriteItem(pid);if(!item)return;const post=item.post||{{}};
  document.getElementById('favorite-modal-pid').textContent=String(pid);
  const content=document.getElementById('favorite-modal-content');content.replaceChildren();
  const original=element('section','original-post');original.append(element('h4','','原帖'),element('div','',post.text||''));content.append(original);
  const commentsSection=element('section','all-comments');
  const comments=post.comments||[];commentsSection.append(element('h4','',`全部评论（${{comments.length}}）`));
  if(!comments.length)commentsSection.append(element('p','empty-comments','当前没有可读取的评论。'));
  else{{
    const list=element('ol','comment-list');
    comments.forEach((comment,index)=>{{
      const isNew=item.unread&&(index+1)>Number(item.last_seen_replies||0);
      const row=element('li',isNew?'new-comment':'');row.style.background=favoriteAuthorBackground(comment.author||'匿名');
      const head=element('div','comment-head');head.append(element('b','',comment.author||'匿名'),element('span','',`#${{index+1}}`));row.append(head);
      if(isNew)row.append(element('div','new-reply-label','新回复'));
      if(comment.quoted_text){{const quote=element('div','quoted-comment');quote.append(element('div','',`回复 ${{comment.quoted_author||'匿名'}}`),element('p','',comment.quoted_text));row.append(quote);}}
      row.append(element('div','comment-text',comment.text||''));list.append(row);
    }});commentsSection.append(list);
  }}
  content.append(commentsSection);const dialog=document.getElementById('favorite-post-modal');dialog.showModal();
  const firstNew=content.querySelector('.new-comment');if(firstNew)window.setTimeout(()=>firstNew.scrollIntoView({{block:'center'}}),60);
  favoriteRequest('/api/favorites/read',{{pid:String(pid)}}).catch(()=>{{}});
}}
renderFavorites();syncFavorites();window.setInterval(syncFavorites,3000);
document.addEventListener('keydown',event=>{{if(event.key==='Escape')toggleFavoritesDrawer(false);}});
function openPostModal(id){{
  const dialog=document.getElementById(id);
  if(dialog&&!dialog.open){{dialog.showModal();}}
}}
function closePostModal(button){{
  const dialog=button.closest('dialog');
  if(dialog){{dialog.close();}}
}}
function closeOnBackdrop(event){{
  if(event.target===event.currentTarget){{event.currentTarget.close();}}
}}
async function copyPid(button){{
  const pid=button.dataset.pid;
  try{{
    if(navigator.clipboard&&window.isSecureContext){{await navigator.clipboard.writeText(pid);}}
    else{{const box=document.createElement('textarea');box.value=pid;box.style.position='fixed';box.style.opacity='0';document.body.appendChild(box);box.select();document.execCommand('copy');box.remove();}}
    const old=button.textContent;button.textContent='已复制';button.classList.add('copied');
    window.setTimeout(()=>{{button.textContent=old;button.classList.remove('copied');}},1200);
  }}catch(error){{button.textContent='请手动复制';}}
}}
</script>
</body>
</html>"""


def markdown_report_to_html(content: str, title: str) -> str:
    """Small compatibility wrapper for older callers and historical tests."""
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f'<title>{_escape_text(title)}</title></head><body><pre>{html.escape(content)}</pre></body></html>'
    )


def write_report(content: str, directory: Path, generated_at: datetime) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = generated_at.strftime("%Y-%m-%d_%H%M%S")
    html_content = (
        content
        if content.lstrip().lower().startswith("<!doctype html")
        else markdown_report_to_html(content, f"北大树洞日报 · {generated_at.strftime('%Y-%m-%d')}")
    )
    path = directory / f"{stamp}.html"
    path.write_text(html_content, encoding="utf-8")
    return path
