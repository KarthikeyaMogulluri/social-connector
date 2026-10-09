"""Tests for the login helper's local callback server and helpers."""
import threading
import time

import httpx
import pytest

import envstore
import oauth


@pytest.fixture(autouse=True)
def tmp_env(monkeypatch, tmp_path):
    import socket
    monkeypatch.setattr(envstore, "ENV", tmp_path / ".env")
    with socket.socket() as sock:  # ask the OS for a free port so tests never collide
        sock.bind(("localhost", 0))
        monkeypatch.setattr(oauth, "PORT", sock.getsockname()[1])


def fake_browser(monkeypatch, *paths, delay=0.3):
    """Pretend to be the user's browser: hit the given callback paths in order."""
    def open_(url):
        def go():
            time.sleep(delay)
            for p in paths:
                httpx.get(f"http://localhost:{oauth.PORT}{p}", timeout=5)
        threading.Thread(target=go, daemon=True).start()
    monkeypatch.setattr(oauth.webbrowser, "open", open_)


def test_callback_returns_code(monkeypatch):
    fake_browser(monkeypatch, "/callback?code=CODE123&state=S1")
    assert oauth.wait_for_code("http://auth", "S1", timeout=10) == "CODE123"


def test_favicon_request_does_not_consume_the_login(monkeypatch):
    fake_browser(monkeypatch, "/favicon.ico", "/callback?code=OK&state=S1")
    assert oauth.wait_for_code("http://auth", "S1", timeout=10) == "OK"


def test_state_mismatch_is_rejected(monkeypatch):
    fake_browser(monkeypatch, "/callback?code=EVIL&state=WRONG")
    with pytest.raises(SystemExit, match="Login failed"):
        oauth.wait_for_code("http://auth", "S1", timeout=10)


def test_user_denied_access_shows_platform_message(monkeypatch):
    fake_browser(monkeypatch, "/callback?error=access_denied&error_description=User+cancelled&state=S1")
    with pytest.raises(SystemExit, match="User cancelled"):
        oauth.wait_for_code("http://auth", "S1", timeout=10)


def test_times_out_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(oauth.webbrowser, "open", lambda url: None)
    with pytest.raises(SystemExit, match="Timed out"):
        oauth.wait_for_code("http://auth", "S1", timeout=1.5)


def test_check_scrubs_secrets_and_never_prints_urls(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "super-secret-value")
    r = httpx.Response(400, text="invalid secret super-secret-value",
                       request=httpx.Request("GET", "https://g/x?client_secret=super-secret-value"))
    with pytest.raises(SystemExit) as e:
        oauth.check(r)
    assert "super-secret-value" not in str(e.value) and "400" in str(e.value)


def test_missing_app_keys_exit_with_instruction():
    with pytest.raises(SystemExit, match="LINKEDIN_CLIENT_ID missing"):
        oauth.linkedin()


def test_meta_flow_saves_instagram_and_facebook(monkeypatch):
    monkeypatch.setenv("META_APP_ID", "app1")
    monkeypatch.setenv("META_APP_SECRET", "appsecret1")
    monkeypatch.setattr(oauth, "wait_for_code", lambda url, state, timeout=300: "CODE")

    def get(url, params=None, **kw):
        req = httpx.Request("GET", url)
        if url.endswith("/oauth/access_token") and "code" in params:
            return httpx.Response(200, json={"access_token": "short-tok-aaaa"}, request=req)
        if url.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "long-tok-bbbb"}, request=req)
        return httpx.Response(200, json={"data": [
            {"id": "P1", "name": "No IG Page", "access_token": "pt1-xxxxxxx"},
            {"id": "P2", "name": "Shop", "access_token": "pt2-xxxxxxx", "instagram_business_account": {"id": "IG2"}}]},
            request=req)

    monkeypatch.setattr(oauth.httpx, "get", get)
    oauth.meta("2")
    assert envstore.get("FACEBOOK_PAGE_ID") == "P2"
    assert envstore.get("FACEBOOK_PAGE_TOKEN") == "pt2-xxxxxxx"
    assert envstore.get("INSTAGRAM_BUSINESS_ID") == "IG2"
    assert envstore.get("INSTAGRAM_ACCESS_TOKEN") == "long-tok-bbbb"


def test_meta_page_without_instagram_skips_instagram(monkeypatch):
    monkeypatch.setenv("META_APP_ID", "app1")
    monkeypatch.setenv("META_APP_SECRET", "appsecret1")
    monkeypatch.setattr(oauth, "wait_for_code", lambda *a, **k: "CODE")
    monkeypatch.setattr(oauth.httpx, "get", lambda url, params=None, **kw: httpx.Response(
        200, json={"access_token": "tok-aaaaaaaa", "data": [{"id": "P1", "name": "Solo", "access_token": "pt-aaaaaaa"}]},
        request=httpx.Request("GET", url)))
    oauth.meta()
    assert envstore.get("FACEBOOK_PAGE_ID") == "P1" and envstore.get("INSTAGRAM_BUSINESS_ID") == ""


def test_meta_rejects_out_of_range_page(monkeypatch):
    monkeypatch.setenv("META_APP_ID", "a")
    monkeypatch.setenv("META_APP_SECRET", "b")
    monkeypatch.setattr(oauth, "wait_for_code", lambda *a, **k: "CODE")
    monkeypatch.setattr(oauth.httpx, "get", lambda url, params=None, **kw: httpx.Response(
        200, json={"access_token": "tok-aaaaaaaa", "data": [{"id": "P1", "name": "Solo", "access_token": "x"}]},
        request=httpx.Request("GET", url)))
    with pytest.raises(SystemExit, match="Pick a page number"):
        oauth.meta("5")


def test_x_flow_sends_pkce_and_saves_tokens(monkeypatch):
    monkeypatch.setenv("X_CLIENT_ID", "xid")
    seen = {}

    def fake_wait(url, state, timeout=300):
        seen["url"] = url
        return "CODE"

    def post(url, **kw):
        seen["body"] = kw["data"]
        return httpx.Response(200, json={"access_token": "xa-aaaaaaaa", "refresh_token": "xr-aaaaaaaa",
                                         "expires_in": 7200}, request=httpx.Request("POST", url))

    monkeypatch.setattr(oauth, "wait_for_code", fake_wait)
    monkeypatch.setattr(oauth.httpx, "post", post)
    oauth.x()
    assert "code_challenge_method=S256" in seen["url"]
    assert 43 <= len(seen["body"]["code_verifier"]) <= 128
    assert envstore.get("X_ACCESS_TOKEN") == "xa-aaaaaaaa" and envstore.get("X_REFRESH_TOKEN") == "xr-aaaaaaaa"
    assert envstore.get("X_TOKEN_EXPIRES_AT").isdigit()


def test_threads_flow(monkeypatch):
    monkeypatch.setenv("THREADS_APP_ID", "tid")
    monkeypatch.setenv("THREADS_APP_SECRET", "tsecret-aaa")
    monkeypatch.setattr(oauth, "wait_for_code", lambda *a, **k: "CODE")
    monkeypatch.setattr(oauth.httpx, "post", lambda url, **kw: httpx.Response(
        200, json={"access_token": "short-aaaa", "user_id": 12345}, request=httpx.Request("POST", url)))
    monkeypatch.setattr(oauth.httpx, "get", lambda url, **kw: httpx.Response(
        200, json={"access_token": "long-aaaaaa"}, request=httpx.Request("GET", url)))
    oauth.threads()
    assert envstore.get("THREADS_USER_ID") == "12345" and envstore.get("THREADS_ACCESS_TOKEN") == "long-aaaaaa"
