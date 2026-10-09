"""Tests with a fake network: no real accounts or tokens needed."""
import asyncio
import time

import httpx
import pytest

import envstore
import server


class Fake:
    """Records calls; returns canned responses by URL suffix (first match wins)."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for suffix, resp in self.responses.items():
            if url.endswith(suffix):
                if callable(resp):
                    resp = resp(len(self.calls))
                status, body, headers = resp
                if status >= 400:  # mirror the real _request, which raises on errors
                    raise RuntimeError(f"{method} {url} -> {status}: {body}")
                return httpx.Response(status, json=body, headers=headers,
                                      request=httpx.Request(method, url))
        raise AssertionError(f"unexpected request: {method} {url}")  # unknown URL = a real bug


@pytest.fixture(autouse=True)
def tokens(monkeypatch, tmp_path):
    monkeypatch.setattr(envstore, "ENV", tmp_path / ".env")  # never touch the real .env
    for k, v in {"LINKEDIN_ACCESS_TOKEN": "li-token-123", "INSTAGRAM_ACCESS_TOKEN": "ig-token-123",
                 "INSTAGRAM_BUSINESS_ID": "999", "FACEBOOK_PAGE_ID": "555",
                 "FACEBOOK_PAGE_TOKEN": "fb-token-123", "THREADS_ACCESS_TOKEN": "th-token-123",
                 "THREADS_USER_ID": "777", "X_ACCESS_TOKEN": "x-token-1234"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)


def use(monkeypatch, fake):
    monkeypatch.setattr(server, "_request", fake)
    return fake


# ---------- registry / status ----------

def test_all_tools_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert len(names) == 18
    for n in ("connection_status", "linkedin_post", "instagram_post_image", "facebook_page_post",
              "threads_post", "x_post", "x_delete_post", "linkedin_delete_post"):
        assert n in names


def test_every_publisher_has_confirm_default_false():
    for t in asyncio.run(server.mcp.list_tools()):
        if any(w in t.name for w in ("_post", "_reply", "_delete")) and not t.name.endswith("recent_posts"):
            assert t.inputSchema["properties"]["confirm"]["default"] is False, t.name


def test_status_hides_tokens_and_reports_missing(monkeypatch):
    monkeypatch.delenv("LINKEDIN_ACCESS_TOKEN")
    monkeypatch.delenv("X_ACCESS_TOKEN")
    s = server.connection_status()
    assert s["linkedin"] is False and s["x"] is False
    assert s["instagram"] and s["facebook_page"] and s["threads"]
    assert "token-123" not in str(s)


# ---------- the confirm gate: nothing hits the network without confirm ----------

GATED = [
    lambda: server.linkedin_post("hello"),
    lambda: server.linkedin_delete_post("urn:li:share:1"),
    lambda: server.instagram_post_image("https://x.com/a.jpg", "cap"),
    lambda: server.instagram_reply_to_comment("c1", "thanks"),
    lambda: server.facebook_page_post("hi", "https://example.com"),
    lambda: server.threads_post("hi"),
    lambda: server.x_post("hi"),
    lambda: server.x_delete_post("123"),
]


@pytest.mark.parametrize("call", GATED)
def test_no_network_without_confirm(monkeypatch, call):
    fake = use(monkeypatch, Fake())
    assert call()["status"] == "preview_only"
    assert fake.calls == []


def test_x_preview_warns_about_cost(monkeypatch):
    use(monkeypatch, Fake())
    assert "per post" in server.x_post("hi")["cost"]


# ---------- LinkedIn ----------

def test_linkedin_escape_covers_all_reserved_chars():
    assert server._li_escape("a(b)[c]{d}@e#f*g_h~i|j<k>l\\m") == \
        "a\\(b\\)\\[c\\]\\{d\\}\\@e#f\\*g\\_h\\~i\\|j\\<k\\>l\\\\m"
    assert server._li_escape("plain words, 100% safe!") == "plain words, 100% safe!"


def test_linkedin_hashtags_survive_but_lone_hash_is_escaped():
    assert server._li_escape("ship it #coding #AI2026") == "ship it #coding #AI2026"
    assert server._li_escape("# not a tag, C#, #_x, trailing #") == r"\# not a tag, C\#, \#\_x, trailing \#"


def test_linkedin_post_confirmed_escapes_and_authenticates(monkeypatch):
    fake = use(monkeypatch, Fake({"/v2/userinfo": (200, {"sub": "abc"}, {}),
                                  "/rest/posts": (201, {}, {"x-restli-id": "urn:li:share:7"})}))
    out = server.linkedin_post("Launch (beta) @ 5", confirm=True)
    assert out == {"status": "published", "post_id": "urn:li:share:7"}
    _, _, kw = fake.calls[-1]
    assert kw["json"]["author"] == "urn:li:person:abc"
    assert kw["json"]["commentary"] == "Launch \\(beta\\) \\@ 5"
    assert kw["headers"]["Authorization"] == "Bearer li-token-123"
    assert kw["headers"]["LinkedIn-Version"] == server.DEFAULT_LI_VERSION


def test_linkedin_version_is_overridable(monkeypatch):
    monkeypatch.setenv("LINKEDIN_API_VERSION", "209912")
    assert server._li_headers()["LinkedIn-Version"] == "209912"


def test_linkedin_missing_sub_gives_clear_error(monkeypatch):
    use(monkeypatch, Fake({"/v2/userinfo": (200, {}, {})}))
    with pytest.raises(RuntimeError, match="missing 'sub'"):
        server.linkedin_post("hi", confirm=True)


def test_linkedin_delete_encodes_urn_in_path(monkeypatch):
    fake = use(monkeypatch, Fake({"urn%3Ali%3Ashare%3A123": (204, {}, {})}))
    assert server.linkedin_delete_post("urn:li:share:123", confirm=True)["status"] == "deleted"
    assert fake.calls[0][0] == "DELETE"
    assert fake.calls[0][1].endswith("/rest/posts/urn%3Ali%3Ashare%3A123")


@pytest.mark.parametrize("bad", ["../../me", "urn:li:share:1/../x", "123", "urn:li:share:abc", ""])
def test_linkedin_delete_rejects_bad_urn(bad):
    with pytest.raises(ValueError):
        server.linkedin_delete_post(bad, confirm=True)


@pytest.mark.parametrize("text,n", [("", 0), ("   \n", 0), ("x" * 3001, 3001)])
def test_linkedin_rejects_bad_text(text, n):
    with pytest.raises(ValueError):
        server.linkedin_post(text)


def test_linkedin_3000_chars_is_allowed():
    assert server.linkedin_post("x" * 3000)["characters"] == 3000


# ---------- Instagram ----------

def test_instagram_publish_waits_for_container(monkeypatch):
    def status(call_no):
        return (200, {"status_code": "IN_PROGRESS" if call_no < 4 else "FINISHED"}, {})

    fake = use(monkeypatch, Fake({"/999/media": (200, {"id": "cont1"}, {}),
                                  "/cont1": status,
                                  "/999/media_publish": (200, {"id": "media1"}, {})}))
    out = server.instagram_post_image("https://x.com/a.jpg", "cap", confirm=True)
    assert out["media_id"] == "media1"
    methods = [(c[0], c[1].rsplit("/", 1)[1]) for c in fake.calls]
    assert methods[0] == ("POST", "media") and methods[-1] == ("POST", "media_publish")
    assert sum(1 for m in methods if m[1] == "cont1") >= 2  # polled until ready
    assert fake.calls[0][2]["headers"]["Authorization"] == "Bearer ig-token-123"
    assert "access_token" not in str(fake.calls[0][2])  # token not in body/query


def test_instagram_container_error_aborts_before_publish(monkeypatch):
    fake = use(monkeypatch, Fake({"/999/media": (200, {"id": "c"}, {}),
                                  "/c": (200, {"status_code": "ERROR"}, {})}))
    with pytest.raises(RuntimeError, match="ERROR"):
        server.instagram_post_image("https://x.com/a.jpg", "cap", confirm=True)
    assert not any(c[1].endswith("media_publish") for c in fake.calls)


def test_instagram_never_ready_times_out(monkeypatch):
    use(monkeypatch, Fake({"/999/media": (200, {"id": "c"}, {}),
                           "/c": (200, {"status_code": "IN_PROGRESS"}, {})}))
    with pytest.raises(RuntimeError, match="still processing"):
        server.instagram_post_image("https://x.com/a.jpg", "cap", confirm=True)


@pytest.mark.parametrize("url", ["http://x.com/a.jpg", "https://x.com/a b.jpg", "ftp://x/a", "", "javascript:alert(1)"])
def test_instagram_rejects_bad_image_url(url):
    with pytest.raises(ValueError):
        server.instagram_post_image(url, "cap")


def test_instagram_caption_limits():
    with pytest.raises(ValueError):
        server.instagram_post_image("https://x.com/a.jpg", "x" * 2201)
    with pytest.raises(ValueError, match="30 hashtags"):
        server.instagram_post_image("https://x.com/a.jpg", " ".join(f"#t{i}" for i in range(31)))
    assert server.instagram_post_image("https://x.com/a.jpg", " ".join(f"#t{i}" for i in range(30)))["status"] == "preview_only"


@pytest.mark.parametrize("fn", [
    lambda: server.instagram_media_insights("../999"),
    lambda: server.instagram_comments("1/../2"),
    lambda: server.instagram_reply_to_comment("a b", "x"),
    lambda: server.instagram_media_insights(""),
])
def test_instagram_ids_cannot_alter_the_url_path(fn):
    with pytest.raises(ValueError, match="looks invalid"):
        fn()


def test_instagram_reads_use_header_auth_and_clamp(monkeypatch):
    fake = use(monkeypatch, Fake({"/999/media": (200, {"data": []}, {}),
                                  "/999": (200, {"username": "me"}, {}),
                                  "/m1/comments": (200, {"data": []}, {}),
                                  "/m1/insights": (200, {"data": []}, {})}))
    server.instagram_recent_media(limit=9999)
    assert fake.calls[-1][2]["params"]["limit"] == 50
    server.instagram_recent_media(limit=-5)
    assert fake.calls[-1][2]["params"]["limit"] == 1
    assert server.instagram_account_info() == {"username": "me"}
    server.instagram_comments("m1", limit=0)
    server.instagram_media_insights("m1")
    for _, _, kw in fake.calls:
        assert kw["headers"]["Authorization"] == "Bearer ig-token-123"
        assert "access_token" not in kw.get("params", {})


def test_instagram_reply_confirmed(monkeypatch):
    fake = use(monkeypatch, Fake({"/c9/replies": (200, {"id": "r1"}, {})}))
    assert server.instagram_reply_to_comment("c9", "thanks!", confirm=True) == {"status": "replied", "reply_id": "r1"}
    assert fake.calls[0][2]["data"] == {"message": "thanks!"}


def test_graph_version_is_overridable(monkeypatch):
    assert "/v25.0" in server._graph()
    monkeypatch.setenv("META_GRAPH_VERSION", "v26.0")
    assert server._graph().endswith("/v26.0")


# ---------- Facebook Page ----------

def test_facebook_post_confirmed(monkeypatch):
    fake = use(monkeypatch, Fake({"/555/feed": (200, {"id": "555_1"}, {})}))
    out = server.facebook_page_post("Hello", link="https://example.com", confirm=True)
    assert out == {"status": "published", "post_id": "555_1"}
    assert fake.calls[0][2]["data"] == {"message": "Hello", "link": "https://example.com"}
    assert fake.calls[0][2]["headers"]["Authorization"] == "Bearer fb-token-123"


def test_facebook_validation():
    with pytest.raises(ValueError):
        server.facebook_page_post("  ")
    with pytest.raises(ValueError):
        server.facebook_page_post("hi", link="javascript:alert(1)")


def test_facebook_recent(monkeypatch):
    use(monkeypatch, Fake({"/555/posts": (200, {"data": [1]}, {})}))
    assert server.facebook_page_recent_posts(5) == {"data": [1]}


# ---------- Threads ----------

def test_threads_two_step_publish(monkeypatch):
    fake = use(monkeypatch, Fake({"/777/threads": (200, {"id": "c1"}, {}),
                                  "/c1": (200, {"status": "FINISHED"}, {}),
                                  "/777/threads_publish": (200, {"id": "t1"}, {})}))
    assert server.threads_post("hello", confirm=True) == {"status": "published", "post_id": "t1"}
    assert fake.calls[0][2]["data"]["media_type"] == "TEXT"
    assert fake.calls[-1][2]["data"]["creation_id"] == "c1"


def test_threads_limit_and_byte_warning():
    with pytest.raises(ValueError):
        server.threads_post("x" * 501)
    assert "warning" not in server.threads_post("x" * 500)
    assert server.threads_post("😀" * 200)["warning"]  # 200 chars but 800 bytes


def test_threads_recent(monkeypatch):
    use(monkeypatch, Fake({"/777/threads": (200, {"data": []}, {})}))
    assert server.threads_recent_posts() == {"data": []}


# ---------- X ----------

def test_x_post_confirmed(monkeypatch):
    fake = use(monkeypatch, Fake({"/2/tweets": (201, {"data": {"id": "42"}}, {})}))
    assert server.x_post("hi", confirm=True) == {"status": "published", "post_id": "42"}
    assert fake.calls[0][2]["headers"]["Authorization"] == "Bearer x-token-1234"
    assert fake.calls[0][2]["json"] == {"text": "hi"}


def test_x_limits_and_delete_validation():
    with pytest.raises(ValueError):
        server.x_post("x" * 281)
    with pytest.raises(ValueError):
        server.x_delete_post("1/../2", confirm=True)


def test_x_delete(monkeypatch):
    fake = use(monkeypatch, Fake({"/2/tweets/42": (200, {"data": {"deleted": True}}, {})}))
    assert server.x_delete_post("42", confirm=True)["status"] == "deleted"
    assert fake.calls[0][0] == "DELETE"


def test_x_refreshes_expired_token_and_stores_rotated_refresh(monkeypatch):
    monkeypatch.setenv("X_CLIENT_ID", "cid")
    envstore.save("X_REFRESH_TOKEN", "old-refresh-1")
    envstore.save("X_TOKEN_EXPIRES_AT", str(int(time.time()) - 10))
    fake = use(monkeypatch, Fake({
        "/2/oauth2/token": (200, {"access_token": "new-access-9", "refresh_token": "new-refresh-9", "expires_in": 7200}, {}),
        "/2/tweets": (201, {"data": {"id": "1"}}, {})}))
    server.x_post("hi", confirm=True)
    assert fake.calls[0][1].endswith("/2/oauth2/token")
    assert fake.calls[1][2]["headers"]["Authorization"] == "Bearer new-access-9"
    assert envstore.get("X_REFRESH_TOKEN") == "new-refresh-9"  # single-use token rotated
    assert int(envstore.get("X_TOKEN_EXPIRES_AT")) > time.time() + 7000


def test_x_retries_once_after_401(monkeypatch):
    monkeypatch.setenv("X_CLIENT_ID", "cid")
    envstore.save("X_REFRESH_TOKEN", "r1-long-token")
    attempts = {"n": 0}

    def tweets(_):
        attempts["n"] += 1
        return (401, {}, {}) if attempts["n"] == 1 else (201, {"data": {"id": "5"}}, {})

    use(monkeypatch, Fake({"/2/oauth2/token": (200, {"access_token": "fresh-token-1", "expires_in": 7200}, {}),
                           "/2/tweets": tweets}))
    assert server.x_post("hi", confirm=True)["post_id"] == "5"


def test_x_401_without_refresh_token_reports_error(monkeypatch):
    use(monkeypatch, Fake({"/2/tweets": (401, {}, {})}))
    with pytest.raises(RuntimeError, match="401"):
        server.x_post("hi", confirm=True)


# ---------- error handling & secret hygiene ----------

def test_missing_token_gives_clear_error(monkeypatch):
    monkeypatch.delenv("INSTAGRAM_ACCESS_TOKEN")
    with pytest.raises(RuntimeError, match="INSTAGRAM_ACCESS_TOKEN is not set"):
        server.instagram_recent_media()


def test_platform_error_is_readable_and_scrubbed(monkeypatch):
    monkeypatch.setattr(httpx, "request", lambda *a, **k: httpx.Response(
        401, text="bad token li-token-123 expired", request=httpx.Request("GET", "https://x/y")))
    with pytest.raises(RuntimeError) as e:
        server.linkedin_whoami()
    assert "401" in str(e.value) and "expired" in str(e.value)
    assert "li-token-123" not in str(e.value)


def test_rate_limit_message_includes_retry_after(monkeypatch):
    monkeypatch.setattr(httpx, "request", lambda *a, **k: httpx.Response(
        429, text="slow down", headers={"retry-after": "60"}, request=httpx.Request("GET", "https://x/y")))
    with pytest.raises(RuntimeError, match="retry after 60s"):
        server.linkedin_whoami()


def test_network_failure_does_not_leak_token_in_url(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("cannot reach https://h/p?access_token=th-token-123")

    monkeypatch.setattr(httpx, "request", boom)
    with pytest.raises(RuntimeError) as e:
        server.threads_recent_posts()
    assert "th-token-123" not in str(e.value) and "ConnectError" in str(e.value)


# ---------- envstore ----------

def test_envstore_reads_fresh_and_handles_bom(tmp_path):
    envstore.ENV.write_text(chr(0xFEFF) + "FIRST=one" + chr(10) + "SECOND=two" + chr(10), encoding="utf-8")
    assert envstore.get("FIRST") == "one" and envstore.get("SECOND") == "two"
    envstore.save("SECOND", "three")
    assert envstore.get("SECOND") == "three"
    assert envstore.ENV.read_text(encoding="utf-8").count("SECOND=") == 1


def test_envstore_blocks_newline_injection():
    envstore.save("TOK", "abc\nEVIL=1")
    assert envstore.get("EVIL") == ""
    assert envstore.get("TOK") == "abcEVIL=1"


def test_token_saved_while_running_is_used_without_restart(monkeypatch):
    monkeypatch.delenv("LINKEDIN_ACCESS_TOKEN")
    assert server.connection_status()["linkedin"] is False
    envstore.save("LINKEDIN_ACCESS_TOKEN", "fresh-token-from-oauth")
    assert server.connection_status()["linkedin"] is True
