"""A strict local stand-in for LinkedIn, Meta (Instagram + Facebook Page), Threads and X.

It is deliberately picky, following the platforms' documented rules, so a request that the
real service would reject is rejected here too. The real server code and the real httpx
library run against it over real HTTP; only the far end is fake.

Not a promise that live behaviour is identical (that needs real keys: see smoke_live.py),
but it catches wrong URLs, headers, bodies, ordering and token handling.
"""
import base64
import hashlib
import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REAL_SLEEP = time.sleep  # tests patch time.sleep globally; delays injected by faults must stay real

LI_VERSIONS = {f"2025{m:02d}" for m in range(10, 13)} | {f"2026{m:02d}" for m in range(1, 10)}
GRAPH_VERSIONS = {"v24.0", "v25.0", "v26.0"}
LI_RESERVED = set("\\|{}@[]()<>#*_~")


def render_little_text(text: str):
    """Mimic LinkedIn's parser: returns (rendered, truncated_at_index_or_None).
    An unescaped reserved character ends the visible post (the known 'truncation' bug)."""
    out, i = [], 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
        elif c == "#" and re.match(r"\w", text[i + 1:i + 2] or " "):
            m = re.match(r"#\w+", text[i:])
            out.append(m.group(0))
            i += len(m.group(0))
        elif c in LI_RESERVED:
            return "".join(out), i
        else:
            out.append(c)
            i += 1
    return "".join(out), None


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.log = []                 # every request received
        self.faults = []              # injected failures
        self.counter = 1000
        # tokens
        self.tokens = {"li": {"li-valid-token"}, "ig": {"ig-valid-token"}, "fb": {"fb-valid-token"},
                       "th": {"th-valid-token"}, "x": {"x-valid-token"}}
        self.x_refresh_tokens = {"x-refresh-1"}
        # app registrations for the OAuth flows
        self.apps = {"linkedin": ("li-app", "li-secret"), "meta": ("meta-app", "meta-secret"),
                     "threads": ("th-app", "th-secret"), "x": ("x-app", "x-secret")}
        self.codes = {}               # one-time auth codes -> info
        self.pages = [{"id": "page-1", "name": "Mock Page", "access_token": "fb-valid-token",
                       "instagram_business_account": {"id": "17841000"}}]
        # content
        self.li_posts = {}
        self.containers = {}
        self.media = {}
        self.comments = {"m-1": [{"id": "c-1", "text": "nice", "username": "fan", "timestamp": "2026-10-01T00:00:00+0000"}]}
        self.replies = []
        self.page_posts = []
        self.threads_posts = []
        self.tweets = {}
        self.container_polls = 2      # polls before a container becomes FINISHED
        self.insights_reject_combined = False
        self.x_post_limit = None      # after this many tweets -> 429
        self.publish_quota = 0

    def next_id(self):
        with self.lock:
            self.counter += 1
            return self.counter


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 10  # drop idle keep-alive connections so shutdown is quick

    def log_message(self, *a):
        pass

    # ----- plumbing -----
    @property
    def st(self) -> State:
        return self.server.state

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        ctype = self.headers.get("Content-Type", "")
        if "json" in ctype:
            try:
                return json.loads(raw or b"{}")
            except ValueError:
                return {"__bad_json__": True}
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}

    def reply(self, status, body=None, headers=None, raw=None):
        data = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if raw is None and body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def meta_error(self, msg, code=100, status=400, typ="GraphMethodException"):
        self.reply(status, {"error": {"message": msg, "type": typ, "code": code}})

    def bearer(self):
        h = self.headers.get("Authorization", "")
        return h[7:] if h.startswith("Bearer ") else None

    def handle_any(self, method):
        parsed = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        body = self._read() if method in ("POST", "PUT", "PATCH", "DELETE") else {}
        path = parsed.path
        self.st.log.append({"method": method, "path": path, "raw_path": self.path, "query": q, "body": body,
                            "headers": {k.lower(): v for k, v in self.headers.items()}})
        key = f"{method} {path}"
        for f in list(self.st.faults):  # injected failures
            if re.search(f["match"], key):
                if f.get("times") is not None:
                    f["times"] -= 1
                    if f["times"] <= 0:
                        self.st.faults.remove(f)
                if f.get("delay"):
                    REAL_SLEEP(f["delay"])
                if "raw" in f:
                    return self.reply(f.get("status", 200), raw=f["raw"], headers=f.get("headers"))
                if "status" in f:
                    return self.reply(f["status"], f.get("body", {"error": "injected"}), f.get("headers"))
        try:
            self.route(method, path, q, body)
        except Exception as e:  # a mock bug should be loud, not a hang
            self.reply(500, {"mock_error": repr(e)})

    do_GET = lambda self: self.handle_any("GET")
    do_POST = lambda self: self.handle_any("POST")
    do_DELETE = lambda self: self.handle_any("DELETE")

    # ----- routing -----
    def route(self, m, p, q, b):
        for prefix, fn in (("/li-auth", self.linkedin_oauth), ("/fb-auth", self.fb_dialog),
                           ("/threads-auth", self.threads_authorize), ("/threads-oauth", self.threads_oauth),
                           ("/x-auth", self.x_authorize), ("/x", self.x_api),
                           ("/threads", self.threads_api), ("/v2", self.linkedin_api), ("/rest", self.linkedin_api)):
            if p == prefix or p.startswith(prefix + "/"):
                return fn(m, p[len(prefix):] if prefix not in ("/v2", "/rest") else p, q, b)
        if re.match(r"/v\d+\.\d+/", p):
            return self.graph(m, p, q, b)
        self.reply(404, {"error": f"no route {m} {p}"})

    # ----- LinkedIn -----
    def li_check_headers(self):
        ver = self.headers.get("LinkedIn-Version")
        if not ver or ver not in LI_VERSIONS:
            self.reply(400, {"status": 400, "code": "NONEXISTENT_VERSION",
                             "message": f"Requested version {ver} is not active"})
            return False
        if self.headers.get("X-Restli-Protocol-Version") != "2.0.0":
            self.reply(400, {"status": 400, "message": "Missing X-Restli-Protocol-Version"})
            return False
        return True

    def li_auth(self):
        tok = self.bearer()
        if not tok:
            self.reply(401, {"status": 401, "code": "EMPTY_ACCESS_TOKEN", "message": "Empty access token"})
        elif tok not in self.st.tokens["li"]:
            self.reply(401, {"status": 401, "code": "REVOKED_ACCESS_TOKEN", "message": "The token has been revoked or is invalid"})
        else:
            return True
        return False

    def linkedin_api(self, m, p, q, b):
        if p == "/v2/userinfo" and m == "GET":
            if self.li_auth():
                self.reply(200, {"sub": "mock-member", "name": "Mock Member", "email_verified": True})
            return
        if p == "/rest/posts" and m == "POST":
            if not self.li_check_headers() or not self.li_auth():
                return
            allowed = {"author", "commentary", "visibility", "distribution", "lifecycleState",
                       "isReshareDisabledByAuthor", "content", "reshareContext"}
            if b.get("__bad_json__") or set(b) - allowed:
                return self.reply(400, {"status": 400, "message": f"Unknown fields {sorted(set(b) - allowed)}"})
            for need in ("author", "visibility", "distribution", "lifecycleState"):
                if need not in b:
                    return self.reply(400, {"status": 400, "code": "MISSING_FIELD", "message": f"{need} is required"})
            if not re.fullmatch(r"urn:li:(person|organization):[\w-]+", str(b["author"])):
                return self.reply(400, {"status": 400, "code": "INVALID_URN_ID", "message": "bad author urn"})
            if b["visibility"] not in ("PUBLIC", "CONNECTIONS", "LOGGED_IN"):
                return self.reply(400, {"status": 400, "code": "INVALID_VALUE_FOR_FIELD", "message": "visibility"})
            if b["lifecycleState"] != "PUBLISHED":
                return self.reply(400, {"status": 400, "code": "INVALID_VALUE_FOR_FIELD", "message": "lifecycleState"})
            d = b["distribution"]
            if not isinstance(d, dict) or d.get("feedDistribution") not in ("MAIN_FEED", "NONE"):
                return self.reply(400, {"status": 400, "code": "INVALID_VALUE_FOR_FIELD", "message": "feedDistribution"})
            c = b.get("commentary")
            if not isinstance(c, str) or not c.strip():
                return self.reply(400, {"status": 400, "code": "INVALID_VALUE_BLANK_FIELD", "message": "commentary"})
            if len(c) > 3000:
                return self.reply(400, {"status": 400, "code": "FIELD_LENGTH_TOO_LONG", "message": "commentary"})
            rendered, cut = render_little_text(c)
            urn = f"urn:li:share:{self.st.next_id()}"
            self.st.li_posts[urn] = {"raw": c, "rendered": rendered, "truncated_at": cut, "author": b["author"]}
            return self.reply(201, headers={"x-restli-id": urn})
        mo = re.fullmatch(r"/rest/posts/(.+)", p)
        if mo and m == "GET":  # reading someone's post needs r_member_social, which regular apps lack
            if not self.li_check_headers() or not self.li_auth():
                return
            return self.reply(403, {"status": 403, "code": "ACCESS_DENIED", "message": "Not enough permissions"})
        if mo and m == "DELETE":
            if not self.li_check_headers() or not self.li_auth():
                return
            if "%3A" not in self.path.split("?")[0]:
                return self.reply(400, {"status": 400, "message": "URN in path must be URL-encoded"})
            if self.headers.get("X-RestLi-Method") != "DELETE":
                return self.reply(400, {"status": 400, "message": "Missing X-RestLi-Method: DELETE"})
            urn = urllib.parse.unquote(mo.group(1))
            self.st.li_posts.pop(urn, None)  # idempotent: deleting twice is fine
            return self.reply(204)
        self.reply(404, {"status": 404, "message": f"no LinkedIn route {m} {p}"})

    def linkedin_oauth(self, m, p, q, b):
        cid, secret = self.st.apps["linkedin"]
        if p == "/oauth/v2/authorization":
            if q.get("response_type") != "code" or q.get("client_id") != cid or not q.get("redirect_uri") or not q.get("state"):
                return self.reply(400, {"error": "invalid_request"})
            if "w_member_social" not in q.get("scope", ""):
                return self.reply(400, {"error": "scope missing w_member_social"})
            return self.finish_authorize(q, {})
        if p == "/oauth/v2/accessToken" and m == "POST":
            info = self.st.codes.pop(b.get("code"), None)
            if (b.get("grant_type") != "authorization_code" or not info or b.get("client_id") != cid
                    or b.get("client_secret") != secret or b.get("redirect_uri") != info["redirect_uri"]):
                return self.reply(400, {"error": "invalid_grant"})
            self.st.tokens["li"].add("li-new-token")
            return self.reply(200, {"access_token": "li-new-token", "expires_in": 5184000, "scope": info["scope"]})
        self.reply(404, {})

    def finish_authorize(self, q, extra):
        code = f"code-{self.st.next_id()}"
        self.st.codes[code] = {"redirect_uri": q["redirect_uri"], "scope": q.get("scope", ""), **extra}
        loc = q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": code, "state": q["state"]})
        self.reply(302, headers={"Location": loc})

    # ----- Meta Graph (Instagram + Facebook Page) -----
    def graph_token(self, q, b):
        return self.bearer() or q.get("access_token") or (b.get("access_token") if isinstance(b, dict) else None)

    def graph(self, m, p, q, b):
        ver, rest = re.match(r"/(v\d+\.\d+)(/.*)", p).groups()
        if ver not in GRAPH_VERSIONS:
            return self.meta_error(f"Unsupported version {ver}", 2635, 400)
        if rest == "/oauth/access_token":
            return self.meta_token(q)
        tok = self.graph_token(q, b)
        if rest == "/me/accounts":
            if tok not in {"meta-long-token"} | self.st.tokens["ig"]:
                return self.meta_error("Invalid OAuth access token.", 190, 400, "OAuthException")
            return self.reply(200, {"data": self.st.pages})
        parts = rest.strip("/").split("/")
        node, edge = parts[0], parts[1] if len(parts) > 1 else None
        ig_id, page_id = "17841000", "page-1"
        if node == ig_id:
            if tok not in self.st.tokens["ig"]:
                return self.meta_error("Invalid OAuth access token - Cannot parse access token", 190, 400, "OAuthException")
            return self.ig_node(m, edge, q, b)
        if node == page_id:
            if tok not in self.st.tokens["fb"]:
                return self.meta_error("Invalid OAuth access token.", 190, 400, "OAuthException")
            return self.page_node(m, edge, q, b)
        if node in self.st.containers:
            if tok not in self.st.tokens["ig"]:
                return self.meta_error("Invalid OAuth access token.", 190, 400, "OAuthException")
            c = self.st.containers[node]
            if m == "GET":
                if q.get("fields") != "status_code":
                    return self.meta_error("this mock only serves fields=status_code", 100)
                c["polls"] += 1
                if c["status"] == "IN_PROGRESS" and c["polls"] > self.st.container_polls:
                    c["status"] = "FINISHED"
                return self.reply(200, {"status_code": c["status"], "id": node})
        if node.startswith("m-") or node in self.st.media:
            if tok not in self.st.tokens["ig"]:
                return self.meta_error("Invalid OAuth access token.", 190, 400, "OAuthException")
            return self.media_node(m, node, edge, q, b)
        if node.startswith("c-") and edge == "replies" and m == "POST":
            if tok not in self.st.tokens["ig"]:
                return self.meta_error("Invalid OAuth access token.", 190, 400, "OAuthException")
            if not b.get("message"):
                return self.meta_error("message is required", 100)
            self.st.replies.append((node, b["message"]))
            return self.reply(200, {"id": f"r-{self.st.next_id()}"})
        self.meta_error(f"Unsupported request - {m} {rest}", 100)

    def meta_token(self, q):
        cid, secret = self.st.apps["meta"]
        if q.get("grant_type") == "fb_exchange_token":
            if q.get("client_id") != cid or q.get("client_secret") != secret or q.get("fb_exchange_token") != "meta-short-token":
                return self.meta_error("Invalid exchange", 100, 400, "OAuthException")
            self.st.tokens["ig"].add("meta-long-token")
            return self.reply(200, {"access_token": "meta-long-token", "expires_in": 5184000})
        info = self.st.codes.pop(q.get("code"), None)
        if not info or q.get("client_id") != cid or q.get("client_secret") != secret or q.get("redirect_uri") != info["redirect_uri"]:
            return self.meta_error("Invalid verification code format.", 100, 400, "OAuthException")
        return self.reply(200, {"access_token": "meta-short-token", "expires_in": 3600})

    def fb_dialog(self, m, p, q, b):
        if not re.fullmatch(r"/v\d+\.\d+/dialog/oauth", p):
            return self.reply(404, {})
        cid, _ = self.st.apps["meta"]
        need = {"instagram_content_publish", "pages_show_list", "pages_manage_posts"}
        if q.get("client_id") != cid or not need <= set(q.get("scope", "").split(",")):
            return self.reply(400, {"error": "bad dialog request / missing scopes"})
        self.finish_authorize(q, {})

    def ig_node(self, m, edge, q, b):
        st = self.st
        if edge is None and m == "GET":
            return self.reply(200, {"username": "mock_ig", "followers_count": 10, "media_count": len(st.media), "id": "17841000"})
        if edge == "content_publishing_limit" and m == "GET":
            return self.reply(200, {"data": [{"quota_usage": st.publish_quota, "config": {"quota_total": 100}}]})
        if edge == "media" and m == "GET":
            return self.reply(200, {"data": [{"id": k, **v} for k, v in st.media.items()]})
        if edge == "media" and m == "POST":
            url = b.get("image_url", "")
            if not url.startswith("https://"):
                return self.meta_error("Only https image URLs are supported", 9004, 400)
            if not re.search(r"\.jpe?g(\?|$)", url, re.I):
                return self.meta_error("The image format is not supported (JPEG only)", 9004, 400)
            if len(b.get("caption", "")) > 2200:
                return self.meta_error("Caption too long", 100, 400)
            cid = f"cont-{st.next_id()}"
            st.containers[cid] = {"status": "IN_PROGRESS", "polls": 0, "caption": b.get("caption", ""), "image_url": url}
            return self.reply(200, {"id": cid})
        if edge == "media_publish" and m == "POST":
            c = st.containers.get(b.get("creation_id"))
            if not c:
                return self.meta_error("Invalid creation_id", 100, 400)
            if c["status"] == "PUBLISHED":
                return self.meta_error("Media has already been published", 9007, 400)
            if c["status"] != "FINISHED":
                return self.meta_error("Media ID is not available: the media is not ready to be published", 9007, 400)
            c["status"] = "PUBLISHED"
            mid = f"m-{st.next_id()}"
            st.media[mid] = {"caption": c["caption"], "media_type": "IMAGE", "permalink": f"https://instagram.com/p/{mid}"}
            st.publish_quota += 1
            return self.reply(200, {"id": mid})
        self.meta_error(f"Unsupported IG request {m} {edge}", 100)

    def media_node(self, m, node, edge, q, b):
        if edge == "insights" and m == "GET":
            metrics = (q.get("metric") or "").split(",")
            valid = {"reach", "saved", "total_interactions", "likes", "comments", "shares"}
            if node == "m-story" or any(x not in valid for x in metrics):
                return self.meta_error("metric not supported for this media type", 100, 400)
            if self.st.insights_reject_combined and len(metrics) > 1:
                return self.meta_error("(#100) metrics[1] must be one of the following values", 100, 400)
            return self.reply(200, {"data": [{"name": x, "values": [{"value": 1}]} for x in metrics]})
        if edge == "comments" and m == "GET":
            return self.reply(200, {"data": self.st.comments.get(node, [])})
        self.meta_error(f"Unsupported media request {m} {edge}", 100)

    def page_node(self, m, edge, q, b):
        if edge == "feed" and m == "POST":
            if not b.get("message"):
                return self.meta_error("(#100) The parameter message is required", 100, 400)
            if "link" in b and not re.match(r"https?://", b["link"]):
                return self.meta_error("Invalid link", 100, 400)
            pid = f"page-1_{self.st.next_id()}"
            self.st.page_posts.append({"id": pid, **b})
            return self.reply(200, {"id": pid})
        if edge == "posts" and m == "GET":
            return self.reply(200, {"data": self.st.page_posts})
        self.meta_error(f"Unsupported Page request {m} {edge}", 100)

    # ----- Threads -----
    def threads_authorize(self, m, p, q, b):
        cid, _ = self.st.apps["threads"]
        if p != "/oauth/authorize" or q.get("client_id") != cid or "threads_content_publish" not in q.get("scope", ""):
            return self.reply(400, {"error": "bad threads authorize request"})
        self.finish_authorize(q, {})

    def threads_oauth(self, m, p, q, b):
        cid, secret = self.st.apps["threads"]
        if p == "/oauth/access_token" and m == "POST":
            info = self.st.codes.pop(b.get("code"), None)
            if (not info or b.get("client_id") != cid or b.get("client_secret") != secret
                    or b.get("grant_type") != "authorization_code" or b.get("redirect_uri") != info["redirect_uri"]):
                return self.reply(400, {"error": {"message": "invalid code"}})
            return self.reply(200, {"access_token": "th-short-token", "user_id": 7770001})
        if p == "/access_token" and m == "GET":
            if q.get("grant_type") != "th_exchange_token" or q.get("client_secret") != secret or q.get("access_token") != "th-short-token":
                return self.reply(400, {"error": {"message": "invalid exchange"}})
            self.st.tokens["th"].add("th-long-token")
            return self.reply(200, {"access_token": "th-long-token", "expires_in": 5184000})
        self.reply(404, {})

    def threads_api(self, m, p, q, b):
        st = self.st
        mo = re.fullmatch(r"/v1\.0/([\w-]+)(?:/(\w+))?", p)
        if not mo:
            return self.reply(404, {"error": {"message": f"no route {p}"}})
        node, edge = mo.groups()
        tok = q.get("access_token") or b.get("access_token") or self.bearer()
        if tok not in st.tokens["th"]:
            return self.reply(401, {"error": {"message": "Invalid OAuth access token", "code": 190}})
        if node in st.containers:
            c = st.containers[node]
            c["polls"] += 1
            if c["status"] == "IN_PROGRESS" and c["polls"] > st.container_polls:
                c["status"] = "FINISHED"
            return self.reply(200, {"status": c["status"], "id": node})
        if node != "7770001":
            return self.reply(400, {"error": {"message": "unknown user"}})
        if edge == "threads" and m == "POST":
            if b.get("media_type") != "TEXT" or not b.get("text"):
                return self.reply(400, {"error": {"message": "media_type=TEXT and text are required"}})
            if len(b["text"]) > 500:
                return self.reply(400, {"error": {"message": "Text too long"}})
            cid = f"th-cont-{st.next_id()}"
            st.containers[cid] = {"status": "IN_PROGRESS", "polls": 0, "text": b["text"]}
            return self.reply(200, {"id": cid})
        if edge == "threads_publish" and m == "POST":
            c = st.containers.get(b.get("creation_id"))
            if not c or c["status"] != "FINISHED":
                return self.reply(400, {"error": {"message": "Container not ready"}})
            c["status"] = "PUBLISHED"
            tid = f"th-{st.next_id()}"
            st.threads_posts.append({"id": tid, "text": c["text"]})
            return self.reply(200, {"id": tid})
        if edge == "threads" and m == "GET":
            return self.reply(200, {"data": st.threads_posts})
        self.reply(400, {"error": {"message": f"unsupported {m} {edge}"}})

    # ----- X -----
    def x_authorize(self, m, p, q, b):
        cid, _ = self.st.apps["x"]
        scopes = set(q.get("scope", "").split())
        if (p != "/i/oauth2/authorize" or q.get("client_id") != cid or q.get("response_type") != "code"
                or q.get("code_challenge_method") != "S256" or not q.get("code_challenge")
                or not {"tweet.write", "tweet.read", "users.read"} <= scopes):
            return self.reply(400, {"error": "bad X authorize request"})
        self.finish_authorize(q, {"challenge": q["code_challenge"], "offline": "offline.access" in scopes})

    def x_api(self, m, p, q, b):
        st = self.st
        cid, secret = st.apps["x"]
        if p == "/2/oauth2/token" and m == "POST":
            if b.get("grant_type") == "authorization_code":
                info = st.codes.pop(b.get("code"), None)
                if not info or b.get("redirect_uri") != info["redirect_uri"]:
                    return self.reply(400, {"error": "invalid_grant"})
                digest = base64.urlsafe_b64encode(hashlib.sha256(b.get("code_verifier", "").encode()).digest()).decode().rstrip("=")
                if digest != info["challenge"]:
                    return self.reply(400, {"error": "invalid_grant", "error_description": "PKCE verifier mismatch"})
                st.tokens["x"].add("x-new-access")
                out = {"access_token": "x-new-access", "expires_in": 7200, "token_type": "bearer"}
                if info["offline"]:
                    st.x_refresh_tokens.add("x-new-refresh")
                    out["refresh_token"] = "x-new-refresh"
                return self.reply(200, out)
            if b.get("grant_type") == "refresh_token":
                rt = b.get("refresh_token")
                if rt not in st.x_refresh_tokens:  # single use: a reused token is dead
                    return self.reply(400, {"error": "invalid_request", "error_description": "Value passed for the token was invalid."})
                st.x_refresh_tokens.discard(rt)
                n = st.next_id()
                st.tokens["x"] = {f"x-access-{n}"}
                st.x_refresh_tokens.add(f"x-refresh-{n}")
                return self.reply(200, {"access_token": f"x-access-{n}", "refresh_token": f"x-refresh-{n}", "expires_in": 7200})
            return self.reply(400, {"error": "unsupported_grant_type"})
        tok = self.bearer()
        if tok not in st.tokens["x"]:
            return self.reply(401, {"title": "Unauthorized", "status": 401, "detail": "Unauthorized"})
        if p == "/2/users/me" and m == "GET":
            return self.reply(200, {"data": {"id": "9001", "username": "mock_x", "name": "Mock"}})
        if p == "/2/tweets" and m == "POST":
            allowed = {"text", "media", "poll", "reply", "edit_options", "geo", "quote_tweet_id", "reply_settings"}
            if b.get("__bad_json__") or set(b) - allowed:
                return self.reply(400, {"title": "Invalid Request", "detail": f"unknown fields {sorted(set(b) - allowed)}"})
            text = b.get("text")
            if not isinstance(text, str) or not text.strip():
                return self.reply(400, {"title": "Invalid Request", "detail": "text required"})
            if len(text) > 280:
                return self.reply(403, {"title": "Forbidden", "detail": "Tweet needs to be a bit shorter."})
            if any(t["text"] == text for t in st.tweets.values()):
                return self.reply(403, {"title": "Forbidden", "detail": "You are not allowed to create a Tweet with duplicate content."})
            if st.x_post_limit is not None and len(st.tweets) >= st.x_post_limit:
                return self.reply(429, {"title": "Too Many Requests"}, {"retry-after": "30"})
            tid = str(st.next_id())
            st.tweets[tid] = {"text": text}
            return self.reply(201, {"data": {"id": tid, "text": text}})
        mo = re.fullmatch(r"/2/tweets/(\d+)", p)
        if mo and m == "DELETE":
            deleted = st.tweets.pop(mo.group(1), None) is not None
            return self.reply(200, {"data": {"deleted": deleted}})
        self.reply(404, {"title": "Not Found"})


class MockPlatforms:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.state = State()
        self.state = self.httpd.state
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.02), daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def env(self, tokens=True) -> dict:
        """Environment pointing the real server/oauth code at this fake."""
        e = {
            "LINKEDIN_API_BASE": self.base, "GRAPH_API_BASE": self.base, "X_API_BASE": f"{self.base}/x",
            "THREADS_API_BASE": f"{self.base}/threads/v1.0", "THREADS_OAUTH_API": f"{self.base}/threads-oauth",
            "LINKEDIN_AUTH_BASE": f"{self.base}/li-auth", "FACEBOOK_AUTH_BASE": f"{self.base}/fb-auth",
            "THREADS_AUTH_BASE": f"{self.base}/threads-auth", "X_AUTH_BASE": f"{self.base}/x-auth",
            "SOCIAL_HTTP_TIMEOUT": "5",
        }
        if tokens:
            e.update({"LINKEDIN_ACCESS_TOKEN": "li-valid-token", "INSTAGRAM_ACCESS_TOKEN": "ig-valid-token",
                      "INSTAGRAM_BUSINESS_ID": "17841000", "FACEBOOK_PAGE_ID": "page-1",
                      "FACEBOOK_PAGE_TOKEN": "fb-valid-token", "THREADS_ACCESS_TOKEN": "th-valid-token",
                      "THREADS_USER_ID": "7770001", "X_ACCESS_TOKEN": "x-valid-token"})
        return e

    def fault(self, match, **kw):
        self.state.faults.append({"match": match, **kw})

    def requests(self, pattern=""):
        return [r for r in self.state.log if re.search(pattern, f"{r['method']} {r['path']}")]
