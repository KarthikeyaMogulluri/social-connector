"""The most realistic test short of real accounts: launch server.py as its own process
(exactly how Claude launches it), speak the MCP protocol to it over stdio, and let it talk
to the strict fake platforms. Everything Claude would do goes through here."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mock_platforms import MockPlatforms

HERE = Path(__file__).parent
TOKENS = ["li-valid-token", "ig-valid-token", "fb-valid-token", "x-valid-token", "th-valid-token"]


@pytest.fixture
def mock():
    m = MockPlatforms().start()
    m.state.container_polls = 0
    yield m
    m.stop()


def run_session(mock, tmp_path, steps, extra_env=None):
    env = {**os.environ, **mock.env(), "SOCIAL_ENV_FILE": str(tmp_path / ".env"), **(extra_env or {})}
    params = StdioServerParameters(command=sys.executable, args=[str(HERE / "server.py")], env=env, cwd=str(HERE))

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await steps(s)

    return asyncio.run(asyncio.wait_for(go(), timeout=90))


def text(result):
    return "".join(c.text for c in result.content if hasattr(c, "text"))


def test_claude_style_session_across_all_platforms(mock, tmp_path):
    async def steps(s):
        out = {}
        out["tools"] = sorted(t.name for t in (await s.list_tools()).tools)
        out["status"] = json.loads(text(await s.call_tool("connection_status", {})))
        # preview first, as Claude is told to; nothing may be published yet
        out["preview"] = json.loads(text(await s.call_tool("linkedin_post", {"text": "Shipping (v1) #launch"})))
        out["posts_after_preview"] = len(mock.state.li_posts)
        # then confirm
        out["li"] = json.loads(text(await s.call_tool("linkedin_post", {"text": "Shipping (v1) #launch", "confirm": True})))
        out["ig"] = json.loads(text(await s.call_tool("instagram_post_image", {
            "image_url": "https://cdn.example.com/a.jpg", "caption": "hi", "confirm": True})))
        out["fb"] = json.loads(text(await s.call_tool("facebook_page_post", {"message": "hello", "confirm": True})))
        out["th"] = json.loads(text(await s.call_tool("threads_post", {"text": "hello", "confirm": True})))
        out["x"] = json.loads(text(await s.call_tool("x_post", {"text": "hello", "confirm": True})))
        out["bad_arg"] = await s.call_tool("instagram_comments", {"media_id": "../../etc"})
        out["missing_arg"] = await s.call_tool("linkedin_post", {})
        out["string_false"] = json.loads(text(await s.call_tool("x_post", {"text": "string flag", "confirm": "false"})))
        return out

    o = run_session(mock, tmp_path, steps)
    assert len(o["tools"]) == 18
    assert all(o["status"][k] for k in ("linkedin", "instagram", "facebook_page", "threads", "x"))
    assert o["preview"]["status"] == "preview_only" and o["posts_after_preview"] == 0
    assert o["li"]["status"] == "published"
    post = mock.state.li_posts[o["li"]["post_id"]]
    assert post["rendered"] == "Shipping (v1) #launch" and post["truncated_at"] is None
    assert o["ig"]["status"] == o["fb"]["status"] == o["th"]["status"] == o["x"]["status"] == "published"
    assert o["bad_arg"].isError and "looks invalid" in text(o["bad_arg"])
    assert o["missing_arg"].isError
    assert o["string_false"]["status"] == "preview_only"  # "false" must never be treated as true


def test_errors_surface_as_readable_tool_errors_not_crashes(mock, tmp_path):
    async def steps(s):
        bad = await s.call_tool("linkedin_whoami", {})
        still_alive = await s.call_tool("connection_status", {})  # server survived the failure
        return bad, still_alive

    bad, alive = run_session(mock, tmp_path, steps, {"LINKEDIN_ACCESS_TOKEN": "revoked-token-xyz"})
    assert bad.isError and "401" in text(bad)
    assert not alive.isError


def test_no_token_appears_in_any_protocol_output(mock, tmp_path):
    async def steps(s):
        blobs = []
        for name, args in [("linkedin_whoami", {}), ("instagram_account_info", {}), ("x_whoami", {}),
                           ("threads_recent_posts", {}), ("facebook_page_recent_posts", {}),
                           ("instagram_comments", {"media_id": "m-nope"}), ("connection_status", {})]:
            blobs.append(text(await s.call_tool(name, args)))
        return "\n".join(blobs)

    blob = run_session(mock, tmp_path, steps)
    assert all(t not in blob for t in TOKENS)


def test_server_with_no_configuration_starts_and_explains(tmp_path):
    """First run, before any keys exist: tools list, status says nothing is connected, calls explain what to do."""
    env = {k: v for k, v in os.environ.items() if not k.endswith(("_TOKEN", "_ID", "_SECRET"))}
    env["SOCIAL_ENV_FILE"] = str(tmp_path / "none.env")
    params = StdioServerParameters(command=sys.executable, args=[str(HERE / "server.py")], env=env, cwd=str(HERE))

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                status = json.loads(text(await s.call_tool("connection_status", {})))
                err = await s.call_tool("linkedin_whoami", {})
                return status, err

    status, err = asyncio.run(asyncio.wait_for(go(), timeout=60))
    assert not any(status[k] for k in ("linkedin", "instagram", "facebook_page", "threads", "x"))
    assert err.isError and "LINKEDIN_ACCESS_TOKEN is not set" in text(err)
