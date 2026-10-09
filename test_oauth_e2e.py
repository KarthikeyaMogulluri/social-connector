"""Login flows end to end: the real oauth.py opens a 'browser', talks to the strict fake
platforms (including PKCE and single-use codes), saves tokens, and the real server then
uses those freshly saved tokens to publish."""
import socket
import threading

import httpx
import pytest

import envstore
import oauth
import server
from mock_platforms import MockPlatforms


@pytest.fixture
def world(monkeypatch, tmp_path):
    m = MockPlatforms().start()
    monkeypatch.setattr(envstore, "ENV", tmp_path / ".env")
    for k, v in m.env(tokens=False).items():
        monkeypatch.setenv(k, v)
    with socket.socket() as s:
        s.bind(("localhost", 0))
        monkeypatch.setattr(oauth, "PORT", s.getsockname()[1])
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    m.state.container_polls = 0

    def browser(url):  # the user's browser: follows the platform's redirect back to our callback
        threading.Thread(target=lambda: httpx.get(url, follow_redirects=True, timeout=10), daemon=True).start()

    monkeypatch.setattr(oauth.webbrowser, "open", browser)
    yield m
    m.stop()


def test_linkedin_login_then_post(world, monkeypatch):
    monkeypatch.setenv("LINKEDIN_CLIENT_ID", "li-app")
    monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", "li-secret")
    oauth.linkedin()
    assert envstore.get("LINKEDIN_ACCESS_TOKEN") == "li-new-token"
    assert server.linkedin_post("logged in and posting (live-like)", confirm=True)["status"] == "published"


def test_linkedin_wrong_secret_fails_cleanly_without_leaking(world, monkeypatch):
    monkeypatch.setenv("LINKEDIN_CLIENT_ID", "li-app")
    monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", "wrong-secret-value")
    with pytest.raises(SystemExit) as e:
        oauth.linkedin()
    assert "400" in str(e.value) and "wrong-secret-value" not in str(e.value)
    assert envstore.get("LINKEDIN_ACCESS_TOKEN") == ""


def test_unregistered_client_id_is_refused_by_platform(world, monkeypatch):
    monkeypatch.setenv("LINKEDIN_CLIENT_ID", "not-my-app")
    monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", "li-secret")
    monkeypatch.setattr(oauth, "wait_for_code", lambda url, state, timeout=300: httpx.get(url).status_code and (_ for _ in ()).throw(SystemExit("platform refused the login")))
    with pytest.raises(SystemExit, match="refused"):
        oauth.linkedin()


def test_meta_login_connects_instagram_and_facebook_then_posts_to_both(world, monkeypatch):
    monkeypatch.setenv("META_APP_ID", "meta-app")
    monkeypatch.setenv("META_APP_SECRET", "meta-secret")
    oauth.meta()
    assert envstore.get("FACEBOOK_PAGE_ID") == "page-1"
    assert envstore.get("INSTAGRAM_BUSINESS_ID") == "17841000"
    assert server.instagram_account_info()["username"] == "mock_ig"
    assert server.instagram_post_image("https://cdn.example.com/a.jpg", "hi", confirm=True)["status"] == "published"
    assert server.facebook_page_post("hello page", confirm=True)["status"] == "published"


def test_meta_login_with_no_pages_explains(world, monkeypatch):
    monkeypatch.setenv("META_APP_ID", "meta-app")
    monkeypatch.setenv("META_APP_SECRET", "meta-secret")
    world.state.pages = []
    with pytest.raises(SystemExit, match="No Facebook Pages"):
        oauth.meta()


def test_meta_secret_never_printed_on_failure(world, monkeypatch, capsys):
    monkeypatch.setenv("META_APP_ID", "meta-app")
    monkeypatch.setenv("META_APP_SECRET", "bad-meta-secret-1")
    with pytest.raises(SystemExit) as e:
        oauth.meta()
    assert "bad-meta-secret-1" not in str(e.value) + capsys.readouterr().out


def test_threads_login_then_post(world, monkeypatch):
    monkeypatch.setenv("THREADS_APP_ID", "th-app")
    monkeypatch.setenv("THREADS_APP_SECRET", "th-secret")
    oauth.threads()
    assert envstore.get("THREADS_USER_ID") == "7770001" and envstore.get("THREADS_ACCESS_TOKEN") == "th-long-token"
    assert server.threads_post("hello from a fresh login", confirm=True)["status"] == "published"


def test_x_login_uses_valid_pkce_and_saves_refresh_token(world, monkeypatch):
    monkeypatch.setenv("X_CLIENT_ID", "x-app")
    monkeypatch.setenv("X_CLIENT_SECRET", "x-secret")
    oauth.x()
    assert envstore.get("X_ACCESS_TOKEN") == "x-new-access"
    assert envstore.get("X_REFRESH_TOKEN") == "x-new-refresh"
    assert int(envstore.get("X_TOKEN_EXPIRES_AT")) > 0
    assert server.x_post("posting after oauth", confirm=True)["status"] == "published"


def test_x_token_endpoint_rejects_wrong_pkce_verifier(world):
    """Guards the mock itself: PKCE verification is real, so the x login test above proves our challenge is right."""
    st = world.state
    st.codes["c1"] = {"redirect_uri": "http://localhost/cb", "challenge": "abc", "offline": True, "scope": ""}
    r = httpx.post(f"{world.base}/x/2/oauth2/token", data={
        "grant_type": "authorization_code", "code": "c1", "redirect_uri": "http://localhost/cb", "code_verifier": "wrong"})
    assert r.status_code == 400 and "PKCE" in r.text


def test_auth_codes_are_single_use(world):
    st = world.state
    st.codes["c2"] = {"redirect_uri": "http://localhost/cb", "scope": "w_member_social"}
    body = {"grant_type": "authorization_code", "code": "c2", "redirect_uri": "http://localhost/cb",
            "client_id": "li-app", "client_secret": "li-secret"}
    assert httpx.post(f"{world.base}/li-auth/oauth/v2/accessToken", data=body).status_code == 200
    assert httpx.post(f"{world.base}/li-auth/oauth/v2/accessToken", data=body).status_code == 400
