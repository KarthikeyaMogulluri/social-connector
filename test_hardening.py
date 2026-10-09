"""Tests for the fixes from the line-by-line review."""
import asyncio
import json
import os
import re
import socket
import sys
import time
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import envstore
import oauth
import server
from mock_platforms import MockPlatforms

HERE = Path(__file__).parent


@pytest.fixture
def mock(monkeypatch, tmp_path):
    m = MockPlatforms().start()
    monkeypatch.setattr(envstore, "ENV", tmp_path / ".env")
    for k, v in m.env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        monkeypatch.setattr(oauth, "PORT", s.getsockname()[1])
    yield m
    m.stop()


# ---------- source hygiene ----------

def test_no_invisible_or_odd_characters_in_source_files():
    """A raw U+2028 once hid inside a regex; no source file may contain invisible separators."""
    bad = {chr(c) for c in (0x2028, 0x2029, 0x200B, 0x200E, 0x200F, 0xFEFF, 0x85, 0xA0)}
    for f in HERE.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        found = {hex(ord(c)) for c in text if c in bad}
        assert not found, f"{f.name} contains invisible characters {found}"


# ---------- envstore ----------

def test_save_leaves_no_temp_file_and_survives_repeated_writes(tmp_path):
    for i in range(20):
        envstore.save("TOKEN_ONE", f"value-{i}")
    assert not list(tmp_path.glob("*.tmp"))
    assert envstore.get("TOKEN_ONE") == "value-19"
    assert envstore.ENV.read_text(encoding="utf-8").count("TOKEN_ONE=") == 1


def test_redact_handles_a_secret_that_contains_another(monkeypatch):
    monkeypatch.setenv("A_TOKEN", "abcdefgh")
    monkeypatch.setenv("B_TOKEN", "abcdefghij")
    assert envstore.redact("x abcdefghij y") == "x *** y"  # not "***ij"


def test_unicode_line_separators_cannot_split_a_saved_value():
    envstore.save("TOK", "abc" + chr(0x2028) + "EVIL=1" + chr(0x2029) + "def" + chr(0x85) + "ghi")
    assert envstore.get("TOK") == "abcEVIL=1defghi" and envstore.get("EVIL") == ""


# ---------- request behaviour ----------

@pytest.mark.parametrize("bad", ["abc", "-5", "0", "nan", "inf-ish", ""])
def test_garbage_timeout_setting_falls_back_instead_of_crashing(monkeypatch, bad):
    monkeypatch.setenv("SOCIAL_HTTP_TIMEOUT", bad)
    assert server._number("SOCIAL_HTTP_TIMEOUT", 30) == 30


def test_valid_timeout_setting_is_used(monkeypatch):
    monkeypatch.setenv("SOCIAL_HTTP_TIMEOUT", "12.5")
    assert server._number("SOCIAL_HTTP_TIMEOUT", 30) == 12.5


def test_rejected_token_message_tells_you_how_to_reconnect(mock, monkeypatch):
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "revoked-token-xyz")
    with pytest.raises(RuntimeError, match=r"401.*oauth\.py"):
        server.linkedin_whoami()
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "expired-token-abc")
    with pytest.raises(RuntimeError, match=r"190.*oauth\.py"):
        server.instagram_account_info()


def test_timed_out_publish_warns_the_post_may_exist(mock, monkeypatch):
    monkeypatch.setenv("SOCIAL_HTTP_TIMEOUT", "0.3")
    mock.fault(r"POST /rest/posts", delay=1.2, status=201, body={}, times=1)
    with pytest.raises(RuntimeError, match="may have reached the platform"):
        server.linkedin_post("maybe sent", confirm=True)


def test_timed_out_read_does_not_claim_anything_was_sent(mock, monkeypatch):
    monkeypatch.setenv("SOCIAL_HTTP_TIMEOUT", "0.3")
    mock.fault(r"GET /v2/userinfo", delay=1.0, status=200, body={"sub": "x"}, times=3)
    with pytest.raises(RuntimeError) as e:
        server.linkedin_whoami()
    assert "may have reached" not in str(e.value)


def test_linkedin_profile_check_does_not_depend_on_the_posting_version(mock, monkeypatch):
    monkeypatch.setenv("LINKEDIN_API_VERSION", "201001")  # retired version
    assert server.linkedin_whoami()["sub"] == "mock-member"  # still fine
    with pytest.raises(RuntimeError, match="NONEXISTENT_VERSION"):
        server.linkedin_post("x", confirm=True)  # the real failure shows where it belongs
    assert "linkedin-version" not in mock.requests("userinfo")[0]["headers"]


# ---------- X length rules ----------

def test_x_counts_links_as_23_and_emoji_as_2():
    long_url = "https://example.com/" + "a" * 120
    assert server._x_weight("see " + long_url) == 4 + 23
    assert server._x_weight("😀") == 2 and server._x_weight("日本") == 4 and server._x_weight("abc é") == 5
    # a 300-character post is fine on X when most of it is one long link
    post = "read this " + long_url + " " + "x" * 200
    assert len(post) > 300 and server._x_weight(post) <= 280
    assert server.x_post(post)["status"] == "preview_only"


def test_x_rejects_by_weighted_length():
    with pytest.raises(ValueError, match="counts as"):
        server.x_post("😀" * 141)  # 141 chars but weight 282
    assert server.x_post("😀" * 140)["x_length"] == 280


# ---------- server never freezes ----------

def test_slow_instagram_post_does_not_block_other_tools(mock, tmp_path):
    mock.state.container_polls = 8
    env = {**os.environ, **mock.env(), "SOCIAL_ENV_FILE": str(tmp_path / ".env"), "SOCIAL_POLL_DELAY": "0.25"}
    params = StdioServerParameters(command=sys.executable, args=[str(HERE / "server.py")], env=env, cwd=str(HERE))

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                done = {}

                async def call(name, args, label):
                    res = await s.call_tool(name, args)
                    done[label] = time.monotonic()
                    return res

                start = time.monotonic()
                slow = asyncio.create_task(call("instagram_post_image", {
                    "image_url": "https://cdn.example.com/a.jpg", "caption": "slow", "confirm": True}, "slow"))
                await asyncio.sleep(0.4)  # let the slow call get going
                fast = await call("connection_status", {}, "fast")
                slow_result = await slow
                return start, done, fast, slow_result

    start, done, fast, slow_result = asyncio.run(asyncio.wait_for(go(), 90))
    assert not fast.isError and not slow_result.isError
    assert json.loads(slow_result.content[0].text)["status"] == "published"
    assert done["fast"] < done["slow"] - 0.5, "the quick tool had to wait for the slow one"


# ---------- login helper ----------

def test_pasted_address_variants():
    ok = "https://localhost/callback?code=ABC123&state=S1"
    assert oauth.code_from_pasted(ok, "S1") == "ABC123"
    assert oauth.code_from_pasted("  " + ok + "  ", "S1") == "ABC123"
    assert oauth.code_from_pasted("ABC123#_", "S1") == "ABC123"  # bare code, Threads' '#_' suffix
    assert oauth.code_from_pasted("https://localhost/callback?code=ABC123&state=S1#_", "S1") == "ABC123"
    for bad, msg in [("https://localhost/callback?code=X&state=OTHER", "state mismatch"),
                     ("https://localhost/callback?state=S1", "No code"),
                     ("https://localhost/callback?error=access_denied&error_description=No+thanks&state=S1", "No thanks"),
                     ("", "Nothing was pasted")]:
        with pytest.raises(SystemExit, match=msg):
            oauth.code_from_pasted(bad, "S1")


def test_threads_login_over_an_https_redirect_by_pasting_the_address(mock, monkeypatch):
    """Threads may refuse http://localhost, so a registered https URL is used and the address is pasted back."""
    monkeypatch.setenv("OAUTH_REDIRECT_URI", "https://localhost/callback")
    monkeypatch.setenv("THREADS_APP_ID", "th-app")
    monkeypatch.setenv("THREADS_APP_SECRET", "th-secret")
    opened = []
    monkeypatch.setattr(oauth.webbrowser, "open", lambda url: opened.append(url))

    def user_pastes(prompt=""):
        r = httpx.get(opened[-1], follow_redirects=False)  # the browser lands on the https redirect
        assert r.status_code == 302 and r.headers["location"].startswith("https://localhost/callback?code=")
        return r.headers["location"] + "#_"

    monkeypatch.setattr("builtins.input", user_pastes)
    oauth.threads()
    assert envstore.get("THREADS_USER_ID") == "7770001"
    assert server.threads_post("posted after https login", confirm=True)["status"] == "published"


def test_x_login_on_127_0_0_1(mock, monkeypatch):
    """X wants 127.0.0.1, not localhost."""
    monkeypatch.setenv("OAUTH_HOST", "127.0.0.1")
    monkeypatch.setenv("X_CLIENT_ID", "x-app")
    monkeypatch.setenv("X_CLIENT_SECRET", "x-secret")
    assert oauth.redirect() == f"http://127.0.0.1:{oauth.PORT}/callback"
    import threading
    monkeypatch.setattr(oauth.webbrowser, "open", lambda url: threading.Thread(
        target=lambda: httpx.get(url, follow_redirects=True, timeout=10), daemon=True).start())
    oauth.x()
    assert server.x_post("hello from 127.0.0.1 login", confirm=True)["status"] == "published"


def test_non_json_or_incomplete_replies_exit_cleanly():
    html = httpx.Response(200, text="<html>nope</html>", request=httpx.Request("POST", "https://x"))
    with pytest.raises(SystemExit, match="not JSON"):
        oauth.check(html)
    with pytest.raises(SystemExit, match="shape"):
        oauth.check(httpx.Response(200, json=[1], request=httpx.Request("POST", "https://x")))
    with pytest.raises(SystemExit, match="missing 'access_token'"):
        oauth.pick({"token_type": "bearer"}, "access_token", "LinkedIn token")


def test_meta_page_argument_must_be_a_number(monkeypatch):
    monkeypatch.setenv("META_APP_ID", "a")
    monkeypatch.setenv("META_APP_SECRET", "b")
    with pytest.raises(SystemExit, match="must be a number"):
        oauth.meta("two")


def test_bad_port_setting_explains(monkeypatch):
    monkeypatch.setenv("OAUTH_PORT", "not-a-number")
    with pytest.raises(SystemExit, match="OAUTH_PORT"):
        oauth._port()
