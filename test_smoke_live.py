"""The live smoke-test script itself, exercised against the fake platforms."""
import pytest

import envstore
import server
import smoke_live
from mock_platforms import MockPlatforms


@pytest.fixture
def mock(monkeypatch, tmp_path):
    m = MockPlatforms().start()
    monkeypatch.setattr(envstore, "ENV", tmp_path / ".env")
    for k, v in m.env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    yield m
    m.stop()


def statuses(rows):
    return {r[0]: r[1] for r in rows}


def test_everything_connected_passes_and_posts_nothing(mock, capsys):
    assert smoke_live.main(["--x"]) == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out and out.count("PASS") == 8
    assert not [r for r in mock.state.log if r["method"] in ("POST", "DELETE")]  # strictly read-only
    assert mock.state.li_posts == {} and mock.state.media == {} and mock.state.tweets == {}


def test_x_is_opt_in_because_it_costs_money(mock):
    rows = smoke_live.run()
    assert ("X", "SKIP", "pass --x to check (X charges per call)") in rows
    assert not mock.requests("/x/")


def test_unconnected_platforms_are_skipped_not_failed(mock, monkeypatch, capsys):
    for k in ("THREADS_ACCESS_TOKEN", "X_ACCESS_TOKEN", "FACEBOOK_PAGE_TOKEN"):
        monkeypatch.delenv(k)
    assert smoke_live.main([]) == 0
    assert statuses(smoke_live.run())["Threads"] == "SKIP"


def test_nothing_connected_says_what_to_do(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(envstore, "ENV", tmp_path / "none.env")
    for k in list(__import__("os").environ):
        if k.endswith(("_TOKEN", "_ID")):
            monkeypatch.delenv(k)
    assert smoke_live.main([]) == 0
    assert "Nothing is connected yet" in capsys.readouterr().out


def test_expired_linkedin_token_fails_with_advice(mock, monkeypatch, capsys):
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "expired-token-1")
    assert smoke_live.main([]) == 1
    assert "oauth.py linkedin" in capsys.readouterr().out


def test_retired_linkedin_version_is_detected(mock, monkeypatch, capsys):
    monkeypatch.setenv("LINKEDIN_API_VERSION", "201001")
    assert smoke_live.main([]) == 1
    assert "LINKEDIN_API_VERSION" in capsys.readouterr().out


def test_one_failing_platform_does_not_hide_the_others(mock, monkeypatch):
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "dead-token-12")
    rows = smoke_live.run()
    s = statuses(rows)
    assert s["Threads: recent posts"] == "FAIL" and s["Instagram: account info"] == "PASS"
