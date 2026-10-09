"""Property-based tests: instead of hand-picked examples, hypothesis throws hundreds of
random and nasty inputs at the invariants that must always hold."""
import os
import tempfile
from pathlib import Path
from unittest import mock as umock

import httpx
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import envstore
import server
from mock_platforms import MockPlatforms, render_little_text

any_text = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=400)
real_text = any_text.filter(lambda t: t.strip())


@settings(max_examples=500, deadline=None)
@given(any_text)
def test_linkedin_escape_roundtrips_for_any_text(text):
    """Whatever you write must come out exactly as written, never truncated."""
    rendered, cut = render_little_text(server._li_escape(text))
    assert cut is None and rendered == text


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=60))
def test_safe_id_never_lets_path_characters_through(value):
    try:
        out = server._safe_id(value, "id")
    except ValueError:
        return
    assert out == value
    assert not set(value) & set("/\\.?#% \t\r\n:@&=+")


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=120).filter(lambda v: True))
def test_linkedin_delete_urn_validation_is_strict(urn):
    try:
        server.linkedin_delete_post(urn)
    except ValueError:
        return
    import re
    assert re.fullmatch(r"urn:li:(share|ugcPost|activity):\d+", urn)


@settings(max_examples=300, deadline=None)
@given(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=200))
def test_envstore_roundtrips_any_value(value):
    with tempfile.TemporaryDirectory() as d:
        old = envstore.ENV
        envstore.ENV = Path(d) / ".env"
        try:
            envstore.save("KEY_ONE", "keep-me")
            envstore.save("TOKEN", value)
            import re
            expected = "".join(ch for ch in value if not (ord(ch) < 32 or ord(ch) in (0x85, 0x2028, 0x2029)))
            assert envstore.get("TOKEN") == (expected or os.environ.get("TOKEN", ""))
            assert envstore.get("KEY_ONE") == "keep-me"  # other settings survive
            assert envstore.ENV.read_text(encoding="utf-8").count("TOKEN=") == 1
        finally:
            envstore.ENV = old


@settings(max_examples=150, deadline=None)
@given(real_text)
def test_previews_never_touch_the_network(text):
    def boom(*a, **k):
        raise AssertionError("network was called during a preview")

    calls = [
        lambda: server.linkedin_post(text), lambda: server.threads_post(text), lambda: server.x_post(text),
        lambda: server.facebook_page_post(text), lambda: server.instagram_reply_to_comment("c1", text),
        lambda: server.instagram_post_image("https://cdn.example.com/a.jpg", text),
    ]
    with umock.patch.object(httpx, "request", boom):
        for call in calls:
            try:
                out = call()
            except ValueError:
                continue  # input rejected before anything happens: fine
            assert out["status"] == "preview_only"


def test_any_linkedin_text_survives_a_real_http_roundtrip(monkeypatch, tmp_path):
    m = MockPlatforms().start()
    monkeypatch.setattr(envstore, "ENV", tmp_path / ".env")
    for k, v in m.env().items():
        monkeypatch.setenv(k, v)

    @settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(real_text.filter(lambda t: len(t) <= 3000))
    def inner(text):
        urn = server.linkedin_post(text, confirm=True)["post_id"]
        post = m.state.li_posts[urn]
        assert post["truncated_at"] is None and post["rendered"] == text

    try:
        inner()
    finally:
        m.stop()


@pytest.mark.parametrize("n", [1, 2, 7, 50])
def test_clamped_limits_for_list_tools(monkeypatch, n):
    """Whatever number Claude passes, the platform only ever sees 1..50."""
    seen = []
    monkeypatch.setattr(server, "_request", lambda m, u, **kw: (seen.append(kw["params"]["limit"]) or
                                                               httpx.Response(200, json={"data": []}, request=httpx.Request(m, u))))
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "t-12345678")
    monkeypatch.setenv("INSTAGRAM_BUSINESS_ID", "1")
    for lim in (n, -n, n * 1000, 0):
        server.instagram_recent_media(lim)
    assert all(1 <= x <= 50 for x in seen)
