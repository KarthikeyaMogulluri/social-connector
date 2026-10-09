"""End-to-end tests: the REAL server code and the REAL httpx stack talk over real HTTP
to a strict local fake of each platform (mock_platforms.py).

These catch wrong URLs, headers, request bodies, call ordering, retries, error handling
and token hygiene. They cannot prove live behaviour: run smoke_live.py once you have keys.
"""
import concurrent.futures
import time

import httpx
import pytest

import envstore
import server
from mock_platforms import MockPlatforms, render_little_text

JPEG = "https://cdn.example.com/photo.jpg"
TOKENS = ["li-valid-token", "ig-valid-token", "fb-valid-token", "x-valid-token", "th-valid-token"]


@pytest.fixture
def mock(monkeypatch, tmp_path):
    m = MockPlatforms().start()
    monkeypatch.setattr(envstore, "ENV", tmp_path / ".env")
    for k, v in m.env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)  # no real waiting in retry/poll loops
    yield m
    m.stop()


# =============================== LinkedIn ===============================

def test_linkedin_whoami(mock):
    assert server.linkedin_whoami()["sub"] == "mock-member"


def test_linkedin_plain_post_arrives_intact(mock):
    out = server.linkedin_post("Hello world", confirm=True)
    post = mock.state.li_posts[out["post_id"]]
    assert post["rendered"] == "Hello world" and post["truncated_at"] is None
    assert post["author"] == "urn:li:person:mock-member"
    sent = mock.requests("POST /rest/posts")[0]
    assert sent["headers"]["linkedin-version"] == server.DEFAULT_LI_VERSION
    assert sent["headers"]["x-restli-protocol-version"] == "2.0.0"
    assert sent["headers"]["authorization"] == "Bearer li-valid-token"


SPECIAL_TEXTS = [
    "Launch (beta) [v2] {x} @ 5 * _ ~ | < > \\ done",
    "C# and #AI #2026 plus (parens)",
    "emoji 😀 and ünïcode 日本語 and العربية",
    "line1\nline2\n\nline3",
    "100% sure; a & b = c? yes! \"quoted\" 'single'",
    "#_x # alone",
    "\\",
    "((((",
    "@[Evil](urn:li:organization:1)",
    "trailing hash #",
    "x" * 3000,
]


@pytest.mark.parametrize("text", SPECIAL_TEXTS)
def test_linkedin_special_characters_are_not_truncated(mock, text):
    out = server.linkedin_post(text, confirm=True)
    post = mock.state.li_posts[out["post_id"]]
    assert post["truncated_at"] is None
    assert post["rendered"] == text


def test_the_mock_really_punishes_unescaped_text(mock):
    """Proves the escaping tests above mean something: the fake truncates raw text like LinkedIn does."""
    assert render_little_text("Launch (beta) rocks") == ("Launch ", 7)
    r = httpx.post(f"{mock.base}/rest/posts", headers={
        "Authorization": "Bearer li-valid-token", "LinkedIn-Version": "202609", "X-Restli-Protocol-Version": "2.0.0"},
        json={"author": "urn:li:person:x", "commentary": "Launch (beta) rocks", "visibility": "PUBLIC",
              "distribution": {"feedDistribution": "MAIN_FEED"}, "lifecycleState": "PUBLISHED"})
    assert mock.state.li_posts[r.headers["x-restli-id"]]["truncated_at"] == 7


def test_linkedin_old_version_is_rejected_by_platform(mock, monkeypatch):
    monkeypatch.setenv("LINKEDIN_API_VERSION", "201001")
    with pytest.raises(RuntimeError, match="NONEXISTENT_VERSION"):
        server.linkedin_post("hi", confirm=True)
    assert mock.state.li_posts == {}


def test_linkedin_delete_roundtrip_and_idempotent(mock):
    urn = server.linkedin_post("to delete", confirm=True)["post_id"]
    assert urn in mock.state.li_posts
    assert server.linkedin_delete_post(urn, confirm=True)["status"] == "deleted"
    assert urn not in mock.state.li_posts
    server.linkedin_delete_post(urn, confirm=True)  # deleting again must not error
    d = mock.requests("DELETE")[0]
    assert "urn%3Ali%3Ashare%3A" in d["raw_path"] and d["headers"]["x-restli-method"] == "DELETE"


def test_linkedin_revoked_token_gives_actionable_error(mock, monkeypatch):
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "revoked-token-xyz")
    with pytest.raises(RuntimeError, match="401.*revoked"):
        server.linkedin_whoami()


# =============================== Instagram ===============================

def test_instagram_full_publish_flow_and_ordering(mock):
    out = server.instagram_post_image(JPEG, "Hello #world", confirm=True)
    assert out["status"] == "published"
    assert mock.state.media[out["media_id"]]["caption"] == "Hello #world"
    seq = [(r["method"], r["path"].split("/")[-1]) for r in mock.requests("v25.0")]
    assert seq[0] == ("POST", "media")
    assert seq[-1] == ("POST", "media_publish")
    assert sum(1 for m, p in seq if m == "GET" and p.startswith("cont-")) >= 3  # polled until FINISHED
    assert server.instagram_publishing_limit()["data"][0]["quota_usage"] == 1
    assert out["media_id"] in str(server.instagram_recent_media())
    assert server.instagram_account_info()["username"] == "mock_ig"


def test_instagram_rejects_non_jpeg_before_publishing(mock):
    with pytest.raises(RuntimeError, match="JPEG"):
        server.instagram_post_image("https://cdn.example.com/photo.png", "x", confirm=True)
    assert not mock.requests("media_publish") and mock.state.media == {}


def test_instagram_preview_warns_about_non_jpeg():
    assert server.instagram_post_image("https://cdn.example.com/p.png", "x")["warning"]
    assert "warning" not in server.instagram_post_image(JPEG, "x")


def test_instagram_container_that_never_finishes_publishes_nothing(mock):
    mock.state.container_polls = 10_000
    with pytest.raises(RuntimeError, match="still processing"):
        server.instagram_post_image(JPEG, "x", confirm=True)
    assert not mock.requests("media_publish") and mock.state.media == {}


def test_instagram_container_error_status_stops_before_publish(mock):
    mock.fault(r"GET /v25.0/cont-", status=200, body={"status_code": "ERROR"})
    with pytest.raises(RuntimeError, match="ERROR"):
        server.instagram_post_image(JPEG, "x", confirm=True)
    assert not mock.requests("media_publish")


def test_instagram_insights_happy_path(mock):
    media_id = server.instagram_post_image(JPEG, "x", confirm=True)["media_id"]
    names = {d["name"] for d in server.instagram_media_insights(media_id)["data"]}
    assert names == {"reach", "saved", "total_interactions"}


def test_instagram_insights_falls_back_metric_by_metric(mock):
    mock.state.insights_reject_combined = True
    out = server.instagram_media_insights("m-1")
    assert {d["name"] for d in out["data"]} == {"reach", "saved", "total_interactions"}
    assert out["unavailable"] == {}


def test_instagram_insights_unsupported_media_explains(mock):
    with pytest.raises(RuntimeError, match="No insight metrics available"):
        server.instagram_media_insights("m-story")


def test_instagram_bad_token_does_not_trigger_metric_fallback(mock, monkeypatch):
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "expired-token-abc")
    with pytest.raises(RuntimeError, match="190"):
        server.instagram_media_insights("m-1")
    assert len(mock.requests("insights")) == 1  # one try, not four


def test_instagram_comments_and_reply(mock):
    assert server.instagram_comments("m-1")["data"][0]["id"] == "c-1"
    assert server.instagram_reply_to_comment("c-1", "thanks!", confirm=True)["status"] == "replied"
    assert mock.state.replies == [("c-1", "thanks!")]


def test_instagram_retired_graph_version_gives_clear_error(mock, monkeypatch):
    monkeypatch.setenv("META_GRAPH_VERSION", "v19.0")
    with pytest.raises(RuntimeError, match="2635"):
        server.instagram_account_info()


# =============================== Facebook Page ===============================

def test_facebook_post_with_and_without_link(mock):
    a = server.facebook_page_post("Plain post", confirm=True)
    b = server.facebook_page_post("With link", link="https://example.com/x", confirm=True)
    posts = {p["id"]: p for p in mock.state.page_posts}
    assert posts[a["post_id"]]["message"] == "Plain post" and "link" not in posts[a["post_id"]]
    assert posts[b["post_id"]]["link"] == "https://example.com/x"
    assert len(server.facebook_page_recent_posts()["data"]) == 2


def test_facebook_wrong_token_is_reported(mock, monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_TOKEN", "someone-elses-token")
    with pytest.raises(RuntimeError, match="190"):
        server.facebook_page_post("x", confirm=True)


# =============================== Threads ===============================

def test_threads_publish_flow(mock):
    out = server.threads_post("hello threads", confirm=True)
    assert mock.state.threads_posts[0] == {"id": out["post_id"], "text": "hello threads"}
    assert server.threads_recent_posts()["data"][0]["id"] == out["post_id"]


def test_threads_exact_limit_and_over(mock):
    assert server.threads_post("x" * 500, confirm=True)["status"] == "published"
    with pytest.raises(ValueError):
        server.threads_post("x" * 501, confirm=True)


def test_threads_bad_token(mock, monkeypatch):
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "dead-threads-token")
    with pytest.raises(RuntimeError, match="401"):
        server.threads_post("x", confirm=True)


# =============================== X ===============================

def test_x_post_delete_whoami(mock):
    assert server.x_whoami()["data"]["username"] == "mock_x"
    tid = server.x_post("hello x", confirm=True)["post_id"]
    assert mock.state.tweets[tid]["text"] == "hello x"
    assert server.x_delete_post(tid, confirm=True)["status"] == "deleted"
    assert tid not in mock.state.tweets


def test_x_duplicate_and_rate_limit_messages(mock):
    server.x_post("same text", confirm=True)
    with pytest.raises(RuntimeError, match="duplicate content"):
        server.x_post("same text", confirm=True)
    mock.state.x_post_limit = 1
    with pytest.raises(RuntimeError, match="retry after 30s"):
        server.x_post("different text", confirm=True)


def _x_expired(mock, monkeypatch, **extra):
    mock.state.tokens["x"] = {"some-other-token"}  # current token is now invalid
    monkeypatch.setenv("X_CLIENT_ID", "x-app")
    monkeypatch.setenv("X_CLIENT_SECRET", "x-secret")
    monkeypatch.setenv("X_REFRESH_TOKEN", "x-refresh-1")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def test_x_401_triggers_one_refresh_then_retry_and_persists_tokens(mock, monkeypatch):
    _x_expired(mock, monkeypatch)
    assert server.x_post("after refresh", confirm=True)["status"] == "published"
    assert len(mock.requests("oauth2/token")) == 1
    new_refresh = envstore.get("X_REFRESH_TOKEN")
    assert new_refresh != "x-refresh-1" and envstore.get("X_ACCESS_TOKEN").startswith("x-access-")
    server.x_post("second post", confirm=True)  # new token reused, no second refresh
    assert len(mock.requests("oauth2/token")) == 1
    assert "x-refresh-1" not in mock.state.x_refresh_tokens  # the old single-use token is gone


def test_x_expiry_timestamp_refreshes_before_calling(mock, monkeypatch):
    _x_expired(mock, monkeypatch, X_TOKEN_EXPIRES_AT=str(int(time.time()) - 5))
    server.x_whoami()
    assert [r["method"] for r in mock.requests("/2/")][:2] == ["POST", "GET"]  # token first, then API
    assert len(mock.requests("/2/users/me")) == 1  # no wasted 401 round-trip


def test_x_concurrent_calls_refresh_exactly_once(mock, monkeypatch):
    """Refresh tokens are single-use; two simultaneous refreshes would lock the account out."""
    _x_expired(mock, monkeypatch, X_TOKEN_EXPIRES_AT=str(int(time.time()) - 5))
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda _: server.x_whoami(), range(8)))
    assert all(r["data"]["username"] == "mock_x" for r in results)
    assert len(mock.requests("oauth2/token")) == 1


def test_x_dead_refresh_token_gives_clear_error(mock, monkeypatch):
    _x_expired(mock, monkeypatch, X_REFRESH_TOKEN="already-used-token")
    with pytest.raises(RuntimeError, match="400"):
        server.x_post("x", confirm=True)


# =============================== gates ===============================

GATED = [
    lambda: server.linkedin_post("hello"),
    lambda: server.linkedin_delete_post("urn:li:share:1"),
    lambda: server.instagram_post_image(JPEG, "cap"),
    lambda: server.instagram_reply_to_comment("c-1", "thanks"),
    lambda: server.facebook_page_post("hi"),
    lambda: server.threads_post("hi"),
    lambda: server.x_post("hi"),
    lambda: server.x_delete_post("123"),
]


@pytest.mark.parametrize("call", GATED)
def test_gated_tools_send_nothing_without_confirm(mock, call):
    assert call()["status"] == "preview_only"
    assert mock.state.log == []


# =============================== failure injection ===============================

def test_get_is_retried_after_transient_503(mock):
    mock.fault(r"GET /v2/userinfo", status=503, times=2)
    assert server.linkedin_whoami()["sub"] == "mock-member"
    assert len(mock.requests("GET /v2/userinfo")) == 3


def test_get_gives_up_after_three_attempts(mock):
    mock.fault(r"GET /v2/userinfo", status=503, times=10)
    with pytest.raises(RuntimeError, match="503"):
        server.linkedin_whoami()
    assert len(mock.requests("GET /v2/userinfo")) == 3


def test_publishing_post_is_never_retried_so_no_duplicates(mock):
    mock.fault(r"POST /rest/posts", status=503, times=1)
    with pytest.raises(RuntimeError, match="503"):
        server.linkedin_post("only once", confirm=True)
    assert len(mock.requests("POST /rest/posts")) == 1 and mock.state.li_posts == {}


def test_slow_server_times_out_with_readable_error(mock, monkeypatch):
    monkeypatch.setenv("SOCIAL_HTTP_TIMEOUT", "0.3")
    mock.fault(r"GET /v2/userinfo", delay=1.2, status=200, body={"sub": "late"}, times=3)
    with pytest.raises(RuntimeError, match="Timeout"):
        server.linkedin_whoami()


@pytest.mark.parametrize("raw,expect", [
    (b"<html><body>Bad gateway</body></html>", "not JSON"),
    (b"", "not JSON"),
    (b"[1, 2, 3]", "unexpected reply shape"),
    (b"null", "unexpected reply shape"),
])
def test_garbled_200_replies_become_readable_errors(mock, raw, expect):
    mock.fault(r"GET /v2/userinfo", status=200, raw=raw, times=1)
    with pytest.raises(RuntimeError, match=expect):
        server.linkedin_whoami()


def test_missing_id_in_platform_reply_stops_the_flow(mock):
    mock.fault(r"POST /v25.0/17841000/media$", status=200, body={}, times=1)
    with pytest.raises(RuntimeError, match="missing 'id'"):
        server.instagram_post_image(JPEG, "x", confirm=True)
    assert not mock.requests("media_publish")


def test_connection_refused_is_readable_and_leaks_nothing(mock, monkeypatch):
    monkeypatch.setenv("LINKEDIN_API_BASE", "http://127.0.0.1:1")
    with pytest.raises(RuntimeError) as e:
        server.linkedin_whoami()
    assert "ConnectError" in str(e.value) and "li-valid-token" not in str(e.value)


def test_error_body_that_echoes_the_token_is_scrubbed(mock):
    mock.fault(r"GET /v2/userinfo", status=400, body={"echo": "li-valid-token"}, times=1)
    with pytest.raises(RuntimeError) as e:
        server.linkedin_whoami()
    assert "li-valid-token" not in str(e.value) and "***" in str(e.value)


def test_missing_post_id_header_is_not_reported_as_success(mock):
    mock.fault(r"POST /rest/posts", status=201, raw=b"", times=1)
    with pytest.raises(RuntimeError, match="no post id"):
        server.linkedin_post("x", confirm=True)


# =============================== secrets never leak ===============================

def test_no_token_ever_appears_in_outputs_or_urls(mock):
    outputs = []

    def run(fn, *a, **k):
        try:
            outputs.append(str(fn(*a, **k)))
        except Exception as e:
            outputs.append(str(e))

    urn = server.linkedin_post("sweep", confirm=True)["post_id"]
    run(server.connection_status)
    run(server.linkedin_whoami)
    run(server.linkedin_delete_post, urn, confirm=True)
    media = server.instagram_post_image(JPEG, "sweep", confirm=True)["media_id"]
    for fn, a in ((server.instagram_account_info, ()), (server.instagram_recent_media, ()),
                  (server.instagram_media_insights, (media,)), (server.instagram_comments, ("m-1",)),
                  (server.instagram_publishing_limit, ()), (server.facebook_page_post, ("sweep",)),
                  (server.facebook_page_recent_posts, ()), (server.threads_post, ("sweep",)),
                  (server.threads_recent_posts, ()), (server.x_whoami, ()), (server.x_post, ("sweep",)),
                  (server.instagram_media_insights, ("m-story",)), (server.instagram_comments, ("../x",))):
        run(fn, *a, **({"confirm": True} if fn in (server.facebook_page_post, server.threads_post, server.x_post) else {}))
    blob = "\n".join(outputs)
    assert all(t not in blob for t in TOKENS), "a token leaked into a tool output"
    for r in mock.state.log:  # tokens must travel in headers, never in URLs (Threads documents query tokens)
        if not r["path"].startswith("/threads"):
            assert not any(t in r["raw_path"] for t in TOKENS), r["raw_path"]
