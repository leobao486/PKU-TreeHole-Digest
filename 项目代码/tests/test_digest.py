from datetime import datetime, timedelta

import requests

from pku_treehole_digest.auth import _find_treehole_token, make_uuid
from pku_treehole_digest.auth import Credentials
from pku_treehole_digest.client import TreeholeClient
from pku_treehole_digest.cli import _adaptive_select, _find_updated_posts
from pku_treehole_digest.config import (
    AppConfig,
    CategoryConfig,
    DeepSeekConfig,
    FetchConfig,
    FilterConfig,
    OutputConfig,
    ProfileConfig,
)
from pku_treehole_digest.deepseek_classifier import DeepSeekClassifier
from pku_treehole_digest.posts import (
    is_closed_offer,
    is_forced_hot_topic,
    normalize_post,
    score_post,
)
from pku_treehole_digest.report import (
    category_sort_key,
    local_summary,
    post_heading,
    render_report,
    write_report,
)
from pku_treehole_digest.state import load_latest_report_time


def test_normalize_current_v3_shape():
    now = datetime.now().astimezone()
    raw = {
        "pid": 1234567,
        "text": "明晚有人工智能讲座，欢迎报名",
        "timestamp": int(now.timestamp()),
        "likenum": 7,
        "praise_num": 12,
        "reply": 8,
        "comments": [{"text": "地点在理教"}],
    }
    post = normalize_post(raw)
    assert post is not None
    assert post.pid == "1234567"
    assert post.likes == 12
    assert post.favorites == 7
    assert post.comments == ["地点在理教"]


def test_score_matches_category_and_blocks_ads():
    now = datetime.now().astimezone()
    config = FilterConfig(
        min_score=2,
        blocked_keywords=["广告"],
        categories={"讲座活动": CategoryConfig(2.0, ["讲座", "报名"])},
    )
    post = normalize_post(
        {"pid": 1, "text": "今晚讲座开始报名", "timestamp": int((now - timedelta(hours=1)).timestamp())}
    )
    assert post is not None
    scored = score_post(post, config, now)
    assert scored is not None
    assert scored.category == "讲座活动"
    assert scored.score >= 4
    ad = normalize_post({"pid": 2, "text": "广告：讲座报名"})
    assert ad is not None
    assert score_post(ad, config, now) is None


def test_local_summary_removes_links_and_truncates():
    result = local_summary("资料见 https://example.com/abc " + "很重要" * 100, limit=40)
    assert "https://" not in result
    assert len(result) <= 42


def test_closed_offer_is_excluded_but_open_offer_is_kept():
    closed = normalize_post({"pid": 1, "text": "转让一张活动票，已出。"})
    open_offer = normalize_post({"pid": 2, "text": "转让一张活动票，目前仍有效"})
    unrelated = normalize_post({"pid": 3, "text": "课程名额已满"})
    assert closed is not None and open_offer is not None and unrelated is not None
    closed.category = open_offer.category = "资源互助"
    unrelated.category = "学业与课程"
    assert is_closed_offer(closed) is True
    assert is_closed_offer(open_offer) is False
    assert is_closed_offer(unrelated) is False


def test_hot_topic_thresholds_are_twenty_comments_or_twenty_favorites():
    post = normalize_post(
        {
            "pid": 9,
            "text": "hot",
            "reply": 4,
            "praise_num": 8,
            "collect_num": 9,
            "comment_list": [
                {"text": "a", "name_tag": "Alice"},
                {"text": "b", "name_tag": "Bob"},
                {"text": "c", "name_tag": "Carol"},
                {"text": "d", "name_tag": "Dave"},
            ],
        }
    )
    assert post is not None
    assert post.unique_repliers == 4
    assert post.favorites == 9
    assert is_forced_hot_topic(post) is False

    unique_only = normalize_post(
        {
            "pid": 10,
            "text": "not hot by engagement",
            "reply": 4,
            "comment_list": [
                {"text": "a", "name_tag": "A"},
                {"text": "b", "name_tag": "B"},
                {"text": "c", "name_tag": "C"},
                {"text": "d", "name_tag": "D"},
            ],
        }
    )
    assert unique_only is not None
    assert is_forced_hot_topic(unique_only) is False

    replies_only = normalize_post({"pid": 11, "text": "hot by replies", "reply": 20})
    assert replies_only is not None
    assert is_forced_hot_topic(replies_only) is True
    assert "评论=20≥20" in replies_only.heat_reason

    favorites_only = normalize_post({"pid": 12, "text": "hot by favorites", "likenum": 20})
    assert favorites_only is not None
    assert is_forced_hot_topic(favorites_only) is True
    assert "收藏=20≥20" in favorites_only.heat_reason

    below = normalize_post({"pid": 13, "text": "below", "reply": 19, "likenum": 19})
    assert below is not None
    assert is_forced_hot_topic(below) is False

    likes_only = normalize_post(
        {"pid": 14, "text": "likes do not force selection", "praise_num_show": 100}
    )
    assert likes_only is not None
    assert is_forced_hot_topic(likes_only) is False


def test_important_categories_precede_activities_and_post_heading_is_plain_number():
    categories = ["活动与兴趣", "科研与学术", "重要通知", "学业与课程"]
    assert sorted(categories, key=category_sort_key) == [
        "重要通知",
        "学业与课程",
        "科研与学术",
        "活动与兴趣",
    ]
    post = normalize_post({"pid": 123, "text": "test"})
    assert post is not None
    heading = post_heading(post, "08-11 10:00")
    assert heading == "### 123 · 08-11 10:00"
    assert "http" not in heading
    assert "#123" not in heading


def test_auth_helpers_parse_redirect_token_and_make_web_uuid():
    response = requests.Response()
    response.status_code = 200
    response.url = "https://treehole.pku.edu.cn/ch/web/pc/index?token=abc.def-123"
    response._content = b""
    assert _find_treehole_token(response) == "abc.def-123"
    assert len(make_uuid()) == 12


def test_deepseek_json_classification(monkeypatch, tmp_path):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"overview":"有一条课程通知","items":[{"pid":"9","relevance":88,"category":"学习与课程","summary":"选课今晚截止"}]}'
                        }
                    }
                ]
            }

    monkeypatch.setattr("pku_treehole_digest.deepseek_classifier.requests.post", lambda *a, **k: FakeResponse())
    profile = tmp_path / "profile.yaml"
    context = tmp_path / "context.md"
    profile.write_text("近期目标: [完成选课]", encoding="utf-8")
    context.write_text("页面有标签", encoding="utf-8")
    post = normalize_post({"pid": 9, "text": "选课今晚截止"})
    assert post is not None
    result = DeepSeekClassifier("test-key", DeepSeekConfig()).classify([post], profile, context)
    assert result.posts[0].score == 8.8
    assert result.posts[0].category == "学习与课程"
    assert result.posts[0].ai_summary == "选课今晚截止"
    assert result.overview == "有一条课程通知"


def test_sms_verification_uses_current_treehole_endpoints():
    calls = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"success": True}

    class FakeSession:
        headers = {}

        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs.get("json")))
            return FakeResponse()

    client = TreeholeClient(Credentials("token", "uuid"), request_interval=0)
    client.session = FakeSession()
    client.send_sms_code()
    client.verify_sms_code("123456")

    assert calls == [
        ("POST", "https://treehole.pku.edu.cn/chapi/api/jwt_send_msg", None),
        ("POST", "https://treehole.pku.edu.cn/chapi/api/jwt_msg_verify", {"valid_code": "123456"}),
    ]


def test_treehole_timeout_has_actionable_message():
    class TimeoutSession:
        headers = {}

        def request(self, *args, **kwargs):
            raise requests.Timeout("slow")

    client = TreeholeClient(Credentials("token", "uuid"), request_interval=0)
    client.session = TimeoutSession()
    try:
        client.verify_sms_code("123456")
    except RuntimeError as exc:
        assert "响应超时" in str(exc)
        assert "jwt_msg_verify" in str(exc)
    else:
        raise AssertionError("expected a timeout error")


def test_sms_requirement_is_checked_with_feed(monkeypatch):
    client = TreeholeClient(Credentials("token", "uuid"), request_interval=0)
    monkeypatch.setattr(client, "feed_page", lambda **kwargs: [{"pid": 1}])
    assert client.needs_sms_verification() is False

    def requires_sms(**kwargs):
        from pku_treehole_digest.client import AuthenticationError

        raise AuthenticationError("树洞要求额外验证：请手机短信验证")

    monkeypatch.setattr(client, "feed_page", requires_sms)
    assert client.needs_sms_verification() is True


def test_verify_uses_feed_instead_of_unstable_user_info(monkeypatch):
    client = TreeholeClient(Credentials("token", "uuid"), request_interval=0)
    calls = []

    def feed_page(**kwargs):
        calls.append(kwargs)
        return [{"pid": 1}]

    monkeypatch.setattr(client, "feed_page", feed_page)
    assert client.verify() == {"feed_access": True, "sample_count": 1}
    assert calls == [{"page": 1, "limit": 1, "comment_limit": 1}]


def test_feed_since_reads_until_time_boundary_without_page_cap(monkeypatch):
    now = datetime.now().astimezone()
    client = TreeholeClient(Credentials("token", "uuid"), request_interval=0)
    pages = {
        1: [
            {"pid": 4, "text": "new", "timestamp": int(now.timestamp())},
            {"pid": 3, "text": "new", "timestamp": int((now - timedelta(hours=1)).timestamp())},
        ],
        2: [
            {"pid": 2, "text": "new", "timestamp": int((now - timedelta(hours=2)).timestamp())},
            {"pid": 1, "text": "old", "timestamp": int((now - timedelta(hours=5)).timestamp())},
        ],
    }
    calls = []

    def feed_page(page, limit, comment_limit):
        calls.append(page)
        return pages.get(page, [])

    monkeypatch.setattr(client, "feed_page", feed_page)
    result = list(client.iter_feed_since(now - timedelta(hours=3), 25, 5))
    assert [item["pid"] for item in result] == [4, 3, 2]
    assert calls == [1, 2]


def test_post_detail_preserves_total_reply_count_and_comments(monkeypatch):
    client = TreeholeClient(Credentials("token", "uuid"), request_interval=0)
    monkeypatch.setattr(
        client,
        "_get",
        lambda *args, **kwargs: {
            "hole": {"pid": 9, "text": "original", "reply": 1},
            "total": 3,
            "list": [{"text": "new reply"}],
        },
    )
    post = normalize_post(client.post_detail("9"))
    assert post is not None
    assert post.replies == 3
    assert post.comments == ["new reply"]


def test_all_comments_are_paginated_without_a_total_cap(monkeypatch):
    client = TreeholeClient(Credentials("token", "uuid"), request_interval=0)
    pages = {
        1: {"list": [{"cid": 1, "text": "a"}, {"cid": 2, "text": "b"}], "total": 5},
        2: {"list": [{"cid": 3, "text": "c"}, {"cid": 4, "text": "d"}], "total": 5},
        3: {"list": [{"cid": 5, "text": "e"}], "total": 5},
    }
    calls = []

    def get(path, **params):
        calls.append((path, params))
        return pages.get(params["page"], {"list": [], "total": 5})

    monkeypatch.setattr(client, "_get", get)
    result = client.all_comment_items("9", page_size=50)
    assert [item["text"] for item in result] == ["a", "b", "c", "d", "e"]
    assert [params["page"] for _, params in calls] == [1, 2, 3]


def test_rendered_html_has_sidebar_copy_button_collapsed_source_and_counts():
    post = normalize_post(
        {
            "pid": 123456,
            "text": "原帖正文",
            "reply": 2,
            "likenum": 4,
            "praise_num_show": 6,
            "comments": [
                {"text": "第一条评论", "name_tag": "Alice"},
                {
                    "text": "第二条评论",
                    "name_tag": "洞主",
                    "quote": {"text": "第一条评论", "name_tag": "Alice"},
                },
            ],
        }
    )
    assert post is not None
    post.category = "学业与课程"
    post.ai_summary = "这是一条值得关注的课程信息。"
    config = AppConfig(
        fetch=FetchConfig(),
        filters=FilterConfig(),
        deepseek=DeepSeekConfig(),
        profile=ProfileConfig(),
        output=OutputConfig(),
    )
    rendered = render_report([post], 100, datetime.now().astimezone(), config)
    assert 'class="sidebar"' in rendered
    assert 'href="#category-1"' in rendered
    assert 'data-pid="123456"' in rendered
    assert "copyPid(this)" in rendered
    assert '<dialog class="post-modal"' in rendered
    assert "openPostModal('modal-123456')" in rendered
    assert 'class="modal-close"' in rendered
    assert "closeOnBackdrop(event)" in rendered
    assert "原帖正文" in rendered
    assert "第一条评论" in rendered and "第二条评论" in rendered
    assert "Alice" in rendered and "洞主" in rendered
    assert "回复 Alice" in rendered
    assert "--page" in rendered and ".main{width:100%;max-width:1060px;margin:0 auto" in rendered
    assert "热度依据" not in rendered
    assert "入选原因" not in rendered
    assert "建议行动" not in rendered
    assert "<b>评论</b> 2" in rendered
    assert "<b>收藏</b> 4" in rendered
    assert "<b>点赞</b> 6" in rendered


def test_previous_report_post_with_new_replies_is_marked_as_update(monkeypatch):
    client = TreeholeClient(Credentials("token", "uuid"), request_interval=0)
    monkeypatch.setattr(
        client,
        "post_detail",
        lambda pid: {"pid": pid, "text": "tracked", "reply": 5, "comments": [{"text": "new"}]},
    )
    updates = _find_updated_posts(client, {"9": {"replies": 2}})
    assert len(updates) == 1
    assert updates[0].is_update is True
    assert updates[0].update_note == "自上次报告后新增 3 条回复"


def test_adaptive_selection_balances_volume_relevance_and_limits():
    config = DeepSeekConfig(
        minimum_relevance=55,
        fallback_relevance=45,
        high_relevance=75,
        selection_ratio=0.10,
        selection_flex_ratio=0.03,
        selection_min_items=10,
        selection_max_items=300,
    )
    posts = []
    for index in range(500):
        post = normalize_post({"pid": index + 1, "text": "candidate"})
        assert post is not None
        post.score = 6.5 if index < 100 else 5.0
        posts.append(post)
    selected = _adaptive_select(posts, fetched_count=2500, config=config)
    assert len(selected) == 175
    assert all(post.score >= 4.5 for post in selected)

    for index, post in enumerate(posts):
        post.score = 8.0 if index < 400 else 5.0
    assert len(_adaptive_select(posts, fetched_count=2500, config=config)) == 300

    for index, post in enumerate(posts):
        post.score = 6.0 if index < 300 else 5.0
    assert len(_adaptive_select(posts, fetched_count=2500, config=config)) == 250


def test_empty_interval_report_does_not_overwrite_latest(tmp_path):
    first = datetime.now().astimezone()
    write_report("# useful", tmp_path, first, update_latest=True)
    write_report("# empty", tmp_path, first + timedelta(minutes=1), update_latest=False)
    assert "useful" in (tmp_path / "latest.html").read_text(encoding="utf-8")
    timestamped = sorted(path for path in tmp_path.glob("*.html") if path.name != "latest.html")
    assert len(timestamped) == 2
    assert not list(tmp_path.glob("*.md"))


def test_latest_report_time_comes_from_dated_html_and_empty_means_first_run(tmp_path):
    assert load_latest_report_time(tmp_path) is None
    (tmp_path / "latest.html").write_text("alias", encoding="utf-8")
    (tmp_path / "notes.html").write_text("ignored", encoding="utf-8")
    assert load_latest_report_time(tmp_path) is None
    (tmp_path / "2026-08-10_120000.html").write_text("old", encoding="utf-8")
    (tmp_path / "2026-08-11_083015.html").write_text("new", encoding="utf-8")
    latest = load_latest_report_time(tmp_path)
    assert latest is not None
    assert latest.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-11 08:30:15"
