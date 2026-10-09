"""One-time login helper: gets a token and writes it into .env.

Usage:
    python oauth.py linkedin
    python oauth.py meta [page-number]   # Instagram + Facebook Page (one login)
    python oauth.py threads
    python oauth.py x

Needs APP keys in .env first (see README.md).

Redirect URL to register in the developer app: http://localhost:38765/callback
Settings (all optional, in .env):
    OAUTH_PORT          change the local port (must match the registered URL)
    OAUTH_HOST          use 127.0.0.1 instead of localhost (X requires this)
    OAUTH_REDIRECT_URI  use any other registered URL, e.g. an https one (Threads may require
                        https). Then the helper cannot catch the login itself: after you
                        approve, copy the full address from the browser and paste it here.
"""
import base64
import hashlib
import http.server
import secrets
import sys
import time
import urllib.parse
import webbrowser

import httpx

import envstore
from envstore import get as env



def _port() -> int:
    try:
        return int(env("OAUTH_PORT") or 38765)  # 8765 was already taken on this machine
    except ValueError:
        sys.exit("OAUTH_PORT in .env must be a number, for example 38765.")


PORT = _port()


def host() -> str:
    return env("OAUTH_HOST") or "localhost"


def manual_mode() -> bool:
    return bool(env("OAUTH_REDIRECT_URI"))


def redirect() -> str:
    return env("OAUTH_REDIRECT_URI") or f"http://{host()}:{PORT}/callback"


def base(name: str, default: str) -> str:
    """Endpoint base URL; overridable so tests can use a local fake service."""
    return (env(name) or default).rstrip("/")


def need(name: str) -> str:
    v = env(name)
    if not v:
        sys.exit(f"{name} missing in .env. Add your developer-app keys first.")
    return v


def check(r: httpx.Response) -> dict:
    """Return JSON or exit with a scrubbed message (never prints URLs, which can hold secrets)."""
    if r.status_code >= 400:
        sys.exit(envstore.redact(f"Request failed ({r.status_code}): {r.text[:300]}"))
    try:
        data = r.json()
    except ValueError:
        sys.exit(envstore.redact(f"Unexpected reply (not JSON, status {r.status_code}): {r.text[:200]!r}"))
    if not isinstance(data, dict):
        sys.exit("Unexpected reply shape from the platform.")
    return data


def pick(data: dict, key: str, what: str):
    """Fetch a required field or exit with a clear message instead of a traceback."""
    if data.get(key) in (None, ""):
        sys.exit(envstore.redact(f"{what} reply is missing '{key}'. It contained: {sorted(data)}"))
    return data[key]


def code_from_pasted(raw: str, state: str) -> str:
    """Manual mode: the user pastes the browser's final address (or just the code)."""
    raw = raw.strip()
    if raw.startswith(("http://", "https://")):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
        if "error" in q:
            sys.exit(f"Login failed: {q.get('error_description', q['error'])[0]}")
        if q.get("state", [""])[0] != state:
            sys.exit("Login failed: that address belongs to a different login attempt (state mismatch).")
        if "code" not in q:
            sys.exit("No code found in that address.")
        return q["code"][0]
    code = raw.split("#")[0]  # Threads appends '#_' to the code
    if not code:
        sys.exit("Nothing was pasted.")
    return code


def wait_for_code(auth_url: str, state: str, timeout: float = 300) -> str:
    """Open the browser, catch the redirect on localhost, return the ?code=.

    Only /callback counts, so a stray favicon request cannot use up the one-shot server.
    """
    if manual_mode():
        print("Opening your browser to log in. If nothing opens, paste this URL into a browser:\n" + auth_url)
        webbrowser.open(auth_url)
        print("\nAfter you approve, the browser may show an error page: that is normal.")
        print("Copy the FULL address from the browser's address bar and paste it below.")
        return code_from_pasted(input("Paste address: "), state)

    box: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            q = urllib.parse.parse_qs(u.query)
            if q.get("state", [""])[0] == state and "code" in q:
                box["code"] = q["code"][0]
                msg = "Connected. You can close this tab."
            else:
                box["error"] = q.get("error_description", ["state mismatch or no code returned"])[0]
                msg = "Login failed. Check the terminal."
            body = msg.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    try:
        server = http.server.HTTPServer((host(), PORT), Handler)
    except OSError:
        sys.exit(f"Port {PORT} is busy. Close whatever uses it and retry.")
    server.timeout = 1
    print("Opening your browser to log in. If nothing opens, paste this URL into a browser:\n" + auth_url)
    webbrowser.open(auth_url)
    deadline = time.time() + timeout
    while not box and time.time() < deadline:
        server.handle_request()
    server.server_close()
    if not box:
        sys.exit("Timed out waiting for login.")
    if "error" in box:
        sys.exit(f"Login failed: {box['error']}")
    return box["code"]


def linkedin() -> None:
    cid, secret = need("LINKEDIN_CLIENT_ID"), need("LINKEDIN_CLIENT_SECRET")
    state = secrets.token_urlsafe(16)
    url = base("LINKEDIN_AUTH_BASE", "https://www.linkedin.com") + "/oauth/v2/authorization?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": cid, "redirect_uri": redirect(),
        "state": state, "scope": "openid profile w_member_social"})
    code = wait_for_code(url, state)
    d = check(httpx.post(base("LINKEDIN_AUTH_BASE", "https://www.linkedin.com") + "/oauth/v2/accessToken", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect(),
        "client_id": cid, "client_secret": secret}, timeout=30))
    envstore.save("LINKEDIN_ACCESS_TOKEN", pick(d, "access_token", "LinkedIn token"))
    print(f"LinkedIn token saved to .env (valid ~{d.get('expires_in', 0) // 86400} days).")


def meta(page_choice: str = "") -> None:
    """One Facebook login yields Instagram and Facebook Page access."""
    if page_choice and not page_choice.isdigit():
        sys.exit("The page number must be a number, for example: python oauth.py meta 2")
    ver = env("META_GRAPH_VERSION") or "v25.0"
    graph = f"{base('GRAPH_API_BASE', 'https://graph.facebook.com')}/{ver}"
    app_id, secret = need("META_APP_ID"), need("META_APP_SECRET")
    state = secrets.token_urlsafe(16)
    scopes = ("instagram_basic,instagram_content_publish,instagram_manage_comments,"
              "instagram_manage_insights,pages_show_list,pages_read_engagement,pages_manage_posts")
    url = f"{base('FACEBOOK_AUTH_BASE', 'https://www.facebook.com')}/{ver}/dialog/oauth?" + urllib.parse.urlencode({
        "client_id": app_id, "redirect_uri": redirect(), "state": state, "scope": scopes})
    code = wait_for_code(url, state)
    short = check(httpx.get(f"{graph}/oauth/access_token", params={
        "client_id": app_id, "client_secret": secret, "redirect_uri": redirect(), "code": code}, timeout=30))
    long = check(httpx.get(f"{graph}/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": secret,
        "fb_exchange_token": pick(short, "access_token", "Meta token")}, timeout=30))
    pages = check(httpx.get(f"{graph}/me/accounts", params={
        "fields": "name,access_token,instagram_business_account", "limit": 100,
        "access_token": pick(long, "access_token", "Meta long-lived token")}, timeout=30)).get("data", [])
    if not pages:
        sys.exit("No Facebook Pages found for this login.")
    for i, p in enumerate(pages, 1):
        ig = "with Instagram" if p.get("instagram_business_account") else "no Instagram"
        print(f"  {i}. {p['name']} ({ig})")
    idx = int(page_choice) - 1 if page_choice.isdigit() else 0
    if not 0 <= idx < len(pages):
        sys.exit(f"Pick a page number from 1 to {len(pages)}.")
    if len(pages) > 1 and not page_choice:
        print("Several pages found; using #1. Re-run `python oauth.py meta <number>` to pick another.")
    page = pages[idx]
    envstore.save("FACEBOOK_PAGE_ID", page["id"])
    envstore.save("FACEBOOK_PAGE_TOKEN", page["access_token"])
    print(f"Facebook Page '{page['name']}' saved.")
    ig = page.get("instagram_business_account")
    if ig:
        envstore.save("INSTAGRAM_ACCESS_TOKEN", long["access_token"])
        envstore.save("INSTAGRAM_BUSINESS_ID", ig["id"])
        print("Instagram account saved.")
    else:
        print("This page has no linked Instagram Business account; Instagram skipped.")


def threads() -> None:
    cid, secret = need("THREADS_APP_ID"), need("THREADS_APP_SECRET")
    state = secrets.token_urlsafe(16)
    url = base("THREADS_AUTH_BASE", "https://threads.net") + "/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect(), "state": state,
        "scope": "threads_basic,threads_content_publish", "response_type": "code"})
    code = wait_for_code(url, state)
    short = check(httpx.post(base("THREADS_OAUTH_API", "https://graph.threads.net") + "/oauth/access_token", data={
        "client_id": cid, "client_secret": secret, "grant_type": "authorization_code",
        "redirect_uri": redirect(), "code": code}, timeout=30))
    long = check(httpx.get(base("THREADS_OAUTH_API", "https://graph.threads.net") + "/access_token", params={
        "grant_type": "th_exchange_token", "client_secret": secret,
        "access_token": pick(short, "access_token", "Threads token")}, timeout=30))
    envstore.save("THREADS_ACCESS_TOKEN", pick(long, "access_token", "Threads long-lived token"))
    envstore.save("THREADS_USER_ID", str(pick(short, "user_id", "Threads token")))
    print("Threads token and user id saved (valid ~60 days).")


def x() -> None:
    cid = need("X_CLIENT_ID")
    secret = env("X_CLIENT_SECRET")
    state = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    url = base("X_AUTH_BASE", "https://x.com") + "/i/oauth2/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": cid, "redirect_uri": redirect(),
        "scope": "tweet.read tweet.write users.read offline.access", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    code = wait_for_code(url, state)
    d = check(httpx.post(base("X_API_BASE", "https://api.x.com") + "/2/oauth2/token", auth=(cid, secret) if secret else None, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect(),
        "code_verifier": verifier, "client_id": cid}, timeout=30))
    envstore.save("X_ACCESS_TOKEN", pick(d, "access_token", "X token"))
    if d.get("refresh_token"):
        envstore.save("X_REFRESH_TOKEN", d["refresh_token"])
    else:
        print("Note: X returned no refresh token, so the token will expire in ~2 hours. "
              "Enable 'offline.access' / use a User-authentication app type with OAuth 2.0.")
    envstore.save("X_TOKEN_EXPIRES_AT", str(int(time.time()) + int(d.get("expires_in", 7200))))
    print("X token saved. It auto-refreshes while the server runs. Remember X charges per post.")


COMMANDS = {"linkedin": linkedin, "meta": meta, "instagram": meta, "facebook": meta,
            "threads": threads, "x": x}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else ""
    if which not in COMMANDS:
        sys.exit("Usage: python oauth.py linkedin|meta [page-number]|threads|x")
    if which in ("meta", "instagram", "facebook"):
        COMMANDS[which](*sys.argv[2:3])
    else:
        COMMANDS[which]()
