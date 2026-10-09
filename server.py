"""Social connector: an MCP server that lets Claude talk to social media.

Platforms: LinkedIn, Instagram, Facebook Pages, Threads, X.
Tokens come from .env (see envstore.py), never from chat.
Every tool that publishes, replies or deletes needs confirm=True,
so Claude must preview first.
"""
import asyncio
import functools
import re
import threading
import time
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP

import envstore
from envstore import get as _env

mcp = FastMCP("social-connector")


def tool(fn):
    """Register fn as an MCP tool that runs in a worker thread, so a slow call (for example
    waiting for Instagram to process an image) never freezes the whole server.
    Returns fn itself, so tests and other code can still call it directly."""
    @functools.wraps(fn)
    async def runner(*args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    mcp.tool()(runner)
    return fn

# Versions and base URLs are configurable: platforms retire versions every 1-2 years,
# and tests point the bases at a local fake server.
DEFAULT_GRAPH = "v25.0"
DEFAULT_LI_VERSION = "202609"
DEFAULT_THREADS = "https://graph.threads.net/v1.0"


def _graph() -> str:
    base = _env("GRAPH_API_BASE") or "https://graph.facebook.com"
    return f"{base}/{_env('META_GRAPH_VERSION') or DEFAULT_GRAPH}"


def _li() -> str:
    return _env("LINKEDIN_API_BASE") or "https://api.linkedin.com"


def _x() -> str:
    return _env("X_API_BASE") or "https://api.x.com"


def _need(name: str) -> str:
    value = _env(name)
    if not value:
        raise RuntimeError(f"{name} is not set. Add it to .env (run oauth.py or see README.md).")
    return value


IDEMPOTENT = {"GET", "DELETE"}  # safe to retry; POST publishes could duplicate, so never retried


def _number(name: str, default: float) -> float:
    try:
        value = float(_env(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _auth_hint(status: int, text: str) -> str:
    if status == 401 or re.search(r'"code":\s*190|OAuthException', text):
        return " | The token was rejected or has expired: reconnect with `python oauth.py <linkedin|meta|threads|x>`."
    return ""


def _request(method: str, url: str, **kwargs) -> httpx.Response:
    """Single choke point for every network call (tests patch this)."""
    kwargs.setdefault("timeout", _number("SOCIAL_HTTP_TIMEOUT", 30))
    attempts = 3 if method in IDEMPOTENT else 1
    where = f"{method} {url.split('?')[0]}"
    for i in range(attempts):
        try:
            r = httpx.request(method, url, **kwargs)
        except httpx.TransportError as e:  # timeouts, refused connections, DNS
            if i + 1 < attempts:
                time.sleep(0.5 * (i + 1))
                continue
            # A publish that timed out after being sent may still have gone through.
            maybe_sent = method not in IDEMPOTENT and isinstance(
                e, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError, httpx.ReadError))
            note = " The request may have reached the platform: check there before trying again." if maybe_sent else ""
            # httpx errors can contain the full URL; scrub secrets before Claude sees it.
            raise RuntimeError(envstore.redact(f"{where} failed: {type(e).__name__}: {e}.{note}")) from None
        except httpx.HTTPError as e:
            raise RuntimeError(envstore.redact(f"{where} failed: {type(e).__name__}: {e}")) from None
        if r.status_code in (502, 503, 504) and i + 1 < attempts:
            time.sleep(0.5 * (i + 1))
            continue
        break
    if r.status_code >= 400:
        extra = f" (retry after {r.headers['retry-after']}s)" if r.status_code == 429 and "retry-after" in r.headers else ""
        raise RuntimeError(envstore.redact(
            f"{where} -> {r.status_code}{extra}: {r.text[:400]}{_auth_hint(r.status_code, r.text)}"))
    return r


def _json(r: httpx.Response) -> dict:
    """Parse a reply, turning HTML error pages / empty bodies into a readable error."""
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(envstore.redact(
            f"Platform sent a reply that is not JSON (status {r.status_code}): {r.text[:200]!r}")) from None
    if not isinstance(data, dict):
        raise RuntimeError(f"Platform sent an unexpected reply shape: {type(data).__name__}")
    return data


def _field(data: dict, key: str, what: str):
    if key not in data or data[key] in (None, ""):
        raise RuntimeError(f"Platform reply for {what} is missing '{key}': {str(data)[:200]}")
    return data[key]


def _safe_id(value: str, label: str) -> str:
    """IDs go into URL paths; refuse anything that could change the path."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value or ""):
        raise ValueError(f"{label} looks invalid: {value!r}")
    return value


def _preview(**fields) -> dict:
    return {"status": "preview_only", **{k: v for k, v in fields.items() if v is not None},
            "note": "Call again with confirm=True to do it."}


def _bearer(token_name: str) -> dict:
    return {"Authorization": f"Bearer {_need(token_name)}"}


# ---------- Status ----------

@tool
def connection_status() -> dict:
    """Show which accounts are configured. Works with no tokens; never reveals them."""
    return {
        "linkedin": bool(_env("LINKEDIN_ACCESS_TOKEN")),
        "instagram": bool(_env("INSTAGRAM_ACCESS_TOKEN") and _env("INSTAGRAM_BUSINESS_ID")),
        "facebook_page": bool(_env("FACEBOOK_PAGE_TOKEN") and _env("FACEBOOK_PAGE_ID")),
        "threads": bool(_env("THREADS_ACCESS_TOKEN") and _env("THREADS_USER_ID")),
        "x": bool(_env("X_ACCESS_TOKEN")),
        "hint": "Missing ones: run `python oauth.py <linkedin|meta|threads|x>` (see README.md).",
    }


# ---------- LinkedIn ----------

_LI_RESERVED = set("\\|{}@[]()<>#*_~")


def _li_escape(text: str) -> str:
    """LinkedIn parses post text as 'little text'. Unescaped ( [ { @ etc. silently
    truncate or garble the post (and @[..](..) would create mentions).
    Exception: '#word' is left alone because LinkedIn turns it into a real hashtag.
    Escape once, here, and nowhere else."""
    out = []
    for i, c in enumerate(text):
        if c == "#" and i + 1 < len(text) and text[i + 1].isalnum():  # not '_': it gets escaped, which would orphan the #
            out.append(c)
        elif c in _LI_RESERVED:
            out.append("\\" + c)
        else:
            out.append(c)
    return "".join(out)


def _li_headers(**extra) -> dict:
    return {
        **_bearer("LINKEDIN_ACCESS_TOKEN"),
        "X-Restli-Protocol-Version": "2.0.0",
        "LinkedIn-Version": _env("LINKEDIN_API_VERSION") or DEFAULT_LI_VERSION,
        **extra,
    }


@tool
def linkedin_whoami() -> dict:
    """Return the connected LinkedIn member's name and id (OpenID userinfo)."""
    # OpenID userinfo needs only the token; sending no version header keeps this check
    # independent of the posting API version (smoke_live.py tests that separately).
    return _json(_request("GET", f"{_li()}/v2/userinfo", headers=_bearer("LINKEDIN_ACCESS_TOKEN")))


@tool
def linkedin_post(text: str, confirm: bool = False) -> dict:
    """Publish a text post to the connected member's feed (preview unless confirm=True).

    #hashtags work; other special characters are escaped so they show literally.
    """
    if not text.strip():
        raise ValueError("Post text is empty.")
    if len(text) > 3000:
        raise ValueError(f"LinkedIn posts max out at 3000 characters; this is {len(text)}.")
    if not confirm:
        return _preview(text=text, characters=len(text))
    me = linkedin_whoami()
    sub = _field(me, "sub", "LinkedIn profile (check the token has openid + profile scopes)")
    body = {
        "author": f"urn:li:person:{sub}",
        "commentary": _li_escape(text),
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    r = _request("POST", f"{_li()}/rest/posts", headers=_li_headers(), json=body)
    post_id = r.headers.get("x-restli-id")
    if not post_id:
        raise RuntimeError("LinkedIn accepted the request but returned no post id; check your feed before retrying.")
    return {"status": "published", "post_id": post_id}


@tool
def linkedin_delete_post(post_urn: str, confirm: bool = False) -> dict:
    """Delete one of your own LinkedIn posts by urn, e.g. urn:li:share:123 (preview unless confirm=True)."""
    if not re.fullmatch(r"urn:li:(share|ugcPost|activity):\d+", post_urn or ""):
        raise ValueError(f"post_urn must look like urn:li:share:123, got {post_urn!r}")
    if not confirm:
        return _preview(post_urn=post_urn)
    _request("DELETE", f"{_li()}/rest/posts/{quote(post_urn, safe='')}",
             headers=_li_headers(**{"X-RestLi-Method": "DELETE"}))
    return {"status": "deleted", "post_urn": post_urn}


# ---------- Instagram (Graph API, Business/Creator account) ----------

def _ig() -> tuple[str, dict]:
    return _need("INSTAGRAM_BUSINESS_ID"), _bearer("INSTAGRAM_ACCESS_TOKEN")


@tool
def instagram_account_info() -> dict:
    """Username, follower count and post count of the connected Instagram account."""
    uid, h = _ig()
    return _json(_request("GET", f"{_graph()}/{uid}", headers=h, params={
        "fields": "username,name,followers_count,follows_count,media_count"}))


@tool
def instagram_publishing_limit() -> dict:
    """How many API posts you've used in the last 24 hours (Instagram allows about 100)."""
    uid, h = _ig()
    return _json(_request("GET", f"{_graph()}/{uid}/content_publishing_limit", headers=h,
                          params={"fields": "quota_usage,config"}))


@tool
def instagram_recent_media(limit: int = 10) -> dict:
    """List recent posts with caption, type, link and like/comment counts."""
    uid, h = _ig()
    return _json(_request("GET", f"{_graph()}/{uid}/media", headers=h, params={
        "fields": "id,caption,media_type,permalink,like_count,comments_count",
        "limit": max(1, min(limit, 50))}))


@tool
def instagram_media_insights(media_id: str) -> dict:
    """Reach, saves and interactions for one post. If Instagram rejects a metric for this
    media type, each metric is tried separately and failures are listed under 'unavailable'."""
    _, h = _ig()
    url = f"{_graph()}/{_safe_id(media_id, 'media_id')}/insights"
    metrics = ["reach", "saved", "total_interactions"]
    try:
        return _json(_request("GET", url, headers=h, params={"metric": ",".join(metrics)}))
    except RuntimeError as first:
        first_error = str(first)  # 'first' is deleted when this block ends
        # Only a metric problem is worth retrying one-by-one; a bad token or version is not.
        if "-> 400" not in first_error or re.search(r'"code":\s*(190|2635)|OAuthException', first_error):
            raise
    data, unavailable = [], {}
    for m in metrics:
        try:
            data += _json(_request("GET", url, headers=h, params={"metric": m})).get("data", [])
        except RuntimeError as e:
            unavailable[m] = str(e)[:200]
    if not data:
        raise RuntimeError(f"No insight metrics available for this post. First error: {first_error[:200]}")
    return {"data": data, "unavailable": unavailable}


@tool
def instagram_comments(media_id: str, limit: int = 25) -> dict:
    """Read comments on one of your posts."""
    _, h = _ig()
    return _json(_request("GET", f"{_graph()}/{_safe_id(media_id, 'media_id')}/comments", headers=h, params={
        "fields": "id,text,username,timestamp", "limit": max(1, min(limit, 50))}))


@tool
def instagram_reply_to_comment(comment_id: str, text: str, confirm: bool = False) -> dict:
    """Reply publicly to a comment on your post (preview unless confirm=True)."""
    _safe_id(comment_id, "comment_id")
    if not text.strip():
        raise ValueError("Reply text is empty.")
    if not confirm:
        return _preview(comment_id=comment_id, text=text)
    _, h = _ig()
    r = _json(_request("POST", f"{_graph()}/{comment_id}/replies", headers=h, data={"message": text}))
    return {"status": "replied", "reply_id": _field(r, "id", "comment reply")}


def _wait_ready(url: str, headers: dict, params: dict, ok=("FINISHED", "PUBLISHED"),
                tries: int = 48, delay: float | None = None) -> None:
    """Instagram/Threads build the post in the background; publishing too early fails.
    Waits up to ~2 minutes (Instagram's docs suggest polling for up to 5)."""
    delay = delay if delay is not None else _number("SOCIAL_POLL_DELAY", 2.5)
    for _ in range(tries):
        data = _json(_request("GET", url, headers=headers, params=params))
        status = data.get("status_code") or data.get("status")
        if status is None or status in ok:
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Platform could not prepare the post: {status} {data.get('error_message', '')}".strip())
        time.sleep(delay)
    raise RuntimeError("Post was still processing after 2 minutes; nothing was published. Try again shortly.")


@tool
def instagram_post_image(image_url: str, caption: str, confirm: bool = False) -> dict:
    """Publish a photo (public https URL to a JPEG) with a caption (preview unless confirm=True)."""
    if not re.fullmatch(r"https://\S+", image_url or ""):
        raise ValueError("image_url must be a public https:// link with no spaces; Instagram downloads it itself.")
    if len(caption) > 2200:
        raise ValueError(f"Instagram captions max out at 2200 characters; this is {len(caption)}.")
    if len(re.findall(r"#\w+", caption)) > 30:
        raise ValueError("Instagram allows at most 30 hashtags per post.")
    if not confirm:
        looks_jpeg = re.search(r"\.jpe?g(\?|$)", image_url, re.I)
        return _preview(image_url=image_url, caption=caption, characters=len(caption),
                        warning=None if looks_jpeg else "Instagram only accepts JPEG images; this URL does not end in .jpg/.jpeg.")
    uid, h = _ig()
    c = _field(_json(_request("POST", f"{_graph()}/{uid}/media", headers=h,
                              data={"image_url": image_url, "caption": caption})), "id", "Instagram media container")
    _wait_ready(f"{_graph()}/{c}", h, {"fields": "status_code"})
    p = _json(_request("POST", f"{_graph()}/{uid}/media_publish", headers=h, data={"creation_id": c}))
    return {"status": "published", "media_id": _field(p, "id", "Instagram publish")}


# ---------- Facebook Pages ----------

def _fb() -> tuple[str, dict]:
    return _need("FACEBOOK_PAGE_ID"), _bearer("FACEBOOK_PAGE_TOKEN")


@tool
def facebook_page_recent_posts(limit: int = 10) -> dict:
    """List recent posts on your Facebook Page."""
    pid, h = _fb()
    return _json(_request("GET", f"{_graph()}/{pid}/posts", headers=h, params={
        "fields": "id,message,created_time,permalink_url", "limit": max(1, min(limit, 50))}))


@tool
def facebook_page_post(message: str, link: str = "", confirm: bool = False) -> dict:
    """Publish a post (optionally with a link) on your Facebook Page (preview unless confirm=True)."""
    if not message.strip():
        raise ValueError("Post message is empty.")
    if link and not re.fullmatch(r"https?://\S+", link):
        raise ValueError("link must be an http(s) URL with no spaces.")
    if not confirm:
        return _preview(message=message, link=link or None)
    pid, h = _fb()
    data = {"message": message, **({"link": link} if link else {})}
    r = _json(_request("POST", f"{_graph()}/{pid}/feed", headers=h, data=data))
    return {"status": "published", "post_id": _field(r, "id", "Facebook post")}


# ---------- Threads ----------

def _threads() -> tuple[str, str, str]:
    base = _env("THREADS_API_BASE") or DEFAULT_THREADS
    return base, _need("THREADS_USER_ID"), _need("THREADS_ACCESS_TOKEN")


@tool
def threads_recent_posts(limit: int = 10) -> dict:
    """List your recent Threads posts."""
    base, uid, tok = _threads()
    return _json(_request("GET", f"{base}/{uid}/threads", params={
        "fields": "id,text,permalink,timestamp", "limit": max(1, min(limit, 50)), "access_token": tok}))


@tool
def threads_post(text: str, confirm: bool = False) -> dict:
    """Publish a text post to Threads, max 500 characters (preview unless confirm=True)."""
    if not text.strip():
        raise ValueError("Post text is empty.")
    if len(text) > 500:
        raise ValueError(f"Threads posts max out at 500 characters; this is {len(text)}.")
    if not confirm:
        warn = "Emoji/URLs count by UTF-8 bytes on Threads; may be rejected." if len(text.encode()) > 500 else None
        return _preview(text=text, characters=len(text), warning=warn)
    base, uid, tok = _threads()
    c = _field(_json(_request("POST", f"{base}/{uid}/threads",
                              data={"media_type": "TEXT", "text": text, "access_token": tok})), "id", "Threads container")
    _wait_ready(f"{base}/{c}", {}, {"fields": "status", "access_token": tok})
    p = _json(_request("POST", f"{base}/{uid}/threads_publish", data={"creation_id": c, "access_token": tok}))
    return {"status": "published", "post_id": _field(p, "id", "Threads publish")}


# ---------- X (Twitter) ----------

X_COST_NOTE = ("X charges per post via the API (about $0.015, about $0.20 if it contains a link, "
               "as reported for 2026; check your X developer console). There is no free posting tier.")
_URL = re.compile(r"https?://\S+")


def _x_weight(text: str) -> int:
    """Length as X counts it: every link is 23, and most non-Latin characters/emoji are 2."""
    total = 0
    for ch in _URL.sub("x" * 23, text):
        o = ord(ch)
        light = o <= 0x10FF or 0x2000 <= o <= 0x200D or 0x2010 <= o <= 0x201F or 0x2032 <= o <= 0x2037
        total += 1 if light else 2
    return total


_x_lock = threading.Lock()  # refresh tokens are single-use; two simultaneous refreshes would lock you out


def _x_refresh() -> None:
    """X access tokens last ~2 hours. Refresh tokens are single-use: always store the new one."""
    cid, refresh = _need("X_CLIENT_ID"), _need("X_REFRESH_TOKEN")
    auth = (cid, _env("X_CLIENT_SECRET")) if _env("X_CLIENT_SECRET") else None
    r = _json(_request("POST", f"{_x()}/2/oauth2/token", auth=auth, data={
        "grant_type": "refresh_token", "refresh_token": refresh, "client_id": cid}))
    envstore.save("X_ACCESS_TOKEN", _field(r, "access_token", "X token refresh"))
    if r.get("refresh_token"):
        envstore.save("X_REFRESH_TOKEN", r["refresh_token"])
    envstore.save("X_TOKEN_EXPIRES_AT", str(int(time.time()) + int(r.get("expires_in", 7200))))


def _x_call(method: str, path: str, **kw) -> httpx.Response:
    with _x_lock:
        exp = _env("X_TOKEN_EXPIRES_AT")
        if _env("X_REFRESH_TOKEN") and exp.isdigit() and time.time() > int(exp) - 60:
            _x_refresh()
        used = _env("X_ACCESS_TOKEN")
    try:
        return _request(method, f"{_x()}{path}", headers=_bearer("X_ACCESS_TOKEN"), **kw)
    except RuntimeError as e:
        if "-> 401" in str(e) and _env("X_REFRESH_TOKEN"):
            with _x_lock:
                if _env("X_ACCESS_TOKEN") == used:  # nobody else refreshed meanwhile
                    _x_refresh()
            return _request(method, f"{_x()}{path}", headers=_bearer("X_ACCESS_TOKEN"), **kw)
        raise


@tool
def x_whoami() -> dict:
    """Return the connected X account (username and id)."""
    return _json(_x_call("GET", "/2/users/me"))


@tool
def x_post(text: str, confirm: bool = False) -> dict:
    """Publish a post to X, max 280 characters (preview unless confirm=True). Costs money per post."""
    if not text.strip():
        raise ValueError("Post text is empty.")
    weight = _x_weight(text)
    if weight > 280:
        raise ValueError(f"X posts max out at 280 (links count 23, emoji and most non-Latin characters count 2); this counts as {weight}.")
    if not confirm:
        return _preview(text=text, characters=len(text), x_length=weight, cost=X_COST_NOTE)
    r = _json(_x_call("POST", "/2/tweets", json={"text": text}))
    return {"status": "published", "post_id": _field(r.get("data") or {}, "id", "X post")}


@tool
def x_delete_post(post_id: str, confirm: bool = False) -> dict:
    """Delete one of your own X posts (preview unless confirm=True)."""
    _safe_id(post_id, "post_id")
    if not confirm:
        return _preview(post_id=post_id)
    _x_call("DELETE", f"/2/tweets/{post_id}")
    return {"status": "deleted", "post_id": post_id}


if __name__ == "__main__":
    mcp.run()
