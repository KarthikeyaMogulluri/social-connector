# social-connector

An MCP server so Claude can use LinkedIn, Instagram, Facebook Pages, Threads and X.
Python, `mcp<2`, `httpx`.

**Status:** built, line-by-line reviewed, registered with Claude Code, tests passing (see below).
**Not yet proven:** a real call to any platform (needs your developer-app keys). Run `smoke_live.py` right after connecting.

## Setup from scratch
```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt     # macOS/Linux: .venv/bin/python
copy .env.example .env                                       # then fill in app keys (see below)
.venv\Scripts\python -m pytest                               # no accounts needed
```
Register with Claude Code (use absolute paths to your venv's python and to `server.py`):
```
claude mcp add social-connector --scope user -- <path-to>\.venv\Scripts\python.exe <path-to>\server.py
```
`.env` holds secrets and is git-ignored. Never commit it, and never paste tokens into chat.

## Tools (18)
`confirm=True` is required by every tool marked yes; without it the tool only previews.

| Platform | Tool | What it does | confirm |
|---|---|---|---|
| all | `connection_status` | Which accounts are set up (never shows tokens) | |
| LinkedIn | `linkedin_whoami` | Confirms the token works | |
| LinkedIn | `linkedin_post` | Text post, max 3000 chars, #hashtags work | yes |
| LinkedIn | `linkedin_delete_post` | Delete your own post | yes |
| Instagram | `instagram_account_info` | Username, followers, post count | |
| Instagram | `instagram_publishing_limit` | API posts used in last 24h (limit ~100) | |
| Instagram | `instagram_recent_media` | Recent posts + counts | |
| Instagram | `instagram_media_insights` | Reach, saves, interactions | |
| Instagram | `instagram_comments` | Read comments on a post | |
| Instagram | `instagram_reply_to_comment` | Reply to a comment | yes |
| Instagram | `instagram_post_image` | JPEG photo post (https URL, max 2200 chars, 30 hashtags) | yes |
| Facebook | `facebook_page_recent_posts` | Recent Page posts | |
| Facebook | `facebook_page_post` | Post (optional link) on your Page | yes |
| Threads | `threads_recent_posts` | Recent posts | |
| Threads | `threads_post` | Text post, max 500 chars | yes |
| X | `x_whoami` | Connected account | |
| X | `x_post` | Post, max 280 chars. **Costs money per post** | yes |
| X | `x_delete_post` | Delete your own post | yes |

## Files
- `server.py` the MCP server
- `envstore.py` reads/writes `.env` fresh each time, scrubs secrets from errors
- `oauth.py` one-time login helper, writes tokens into `.env`
- `smoke_live.py` read-only live check to run after connecting
- `mock_platforms.py` strict local fake of all five platforms (used by tests)
- `test_*.py` 165 tests: `.venv\Scripts\python -m pytest` (about 2.5 minutes)
- `.env.example` copy to `.env`

## How it was tested (and what that does and does not prove)
| Layer | File | What it checks |
|---|---|---|
| Unit | `test_server.py`, `test_oauth.py` | Every tool's validation, limits, gates, error text |
| Strict-fake end to end | `test_mock_e2e.py` | Real code + real HTTP against a picky fake that follows each platform's documented rules: headers, bodies, ordering, versions, 401/403/429, retries, timeouts, garbled replies, secret leaks |
| Login flows | `test_oauth_e2e.py` | Whole login (including X PKCE, single-use codes) then publishing with the freshly saved token |
| Real protocol | `test_stdio_e2e.py` | Server launched as its own process, driven over MCP exactly like Claude does |
| Random inputs | `test_properties.py` | Hundreds of nasty random inputs against invariants (text never truncated, ids never alter URLs, previews never touch the network, `.env` round-trips) |
| Review fixes | `test_hardening.py` | Hidden-character lint, atomic `.env` writes, timeouts, reconnect hints, X length rules, server not freezing, https/127.0.0.1 logins |
| Smoke script | `test_smoke_live.py` | The live check itself |
| Mutation check | (manual) | 12 deliberate code breakages, every one caught |

**Honest limit:** the fake follows the platforms' published docs. If a platform behaves differently from its docs, only a live call finds it. That is what `smoke_live.py` is for.

## After you register the apps: connect, then prove it
```
.venv\Scripts\python oauth.py linkedin     # or: meta | threads | x
.venv\Scripts\python smoke_live.py         # add --x to include X (costs money)
```
Then the first real post, safely, from Claude:
1. Ask Claude to draft a harmless test post. It previews (nothing is sent).
2. Say yes. Claude calls the tool again with `confirm=True`.
3. Check the platform. Then ask Claude to delete it (`linkedin_delete_post`, `x_delete_post`).

## Registering the developer apps
Default redirect URL: `http://localhost:38765/callback`. It must match exactly (scheme, host, port, path) in the developer app. Three optional `.env` settings change it:

| Setting | Use it when |
|---|---|
| `OAUTH_PORT` | the port is busy (8765 was already taken on this PC) |
| `OAUTH_HOST=127.0.0.1` | **X**: its docs say to use `127.0.0.1`, not `localhost`. Register `http://127.0.0.1:38765/callback` |
| `OAUTH_REDIRECT_URI=https://...` | **Threads** reportedly needs an HTTPS redirect. Register any https URL you control (it does not have to serve anything). The helper then prints the login link; after you approve, copy the full address from the browser and paste it into the terminal |

Set these per login: put the line in `.env`, run the login, remove it again if other platforms need the default.

1. **LinkedIn** (linkedin.com/developers, needs a Company Page): products "Sign In with LinkedIn using OpenID Connect" + "Share on LinkedIn". Put `LINKEDIN_CLIENT_ID/SECRET` in `.env`.
2. **Meta** (developers.facebook.com): one app covers Instagram + Facebook Page. Instagram must be Business/Creator linked to a Page. Put `META_APP_ID/SECRET`; `python oauth.py meta` (add a number to choose among several Pages).
3. **Threads**: Threads use case in a Meta app. `THREADS_APP_ID/SECRET`.
4. **X** (developer.x.com): OAuth 2.0 app with read+write. `X_CLIENT_ID` (+ `X_CLIENT_SECRET` if confidential). Add credit first: no free posting tier.

## Limits and things to know
- LinkedIn: tokens last ~60 days (re-run oauth.py); public API cannot read others' profiles/feeds; special characters are escaped so they show literally (`#word` stays a hashtag; mentions are deliberately impossible).
- Instagram/Facebook: the Instagram token is a 60-day token; when it expires, rerun `python oauth.py meta`. Meta may require app review before posting for accounts other than your own test users. Graph default v25.0 (v26.0 exists): `META_GRAPH_VERSION`. Images must be JPEG; Instagram can take up to ~2 minutes to process; ~100 API posts per 24h.
- Threads: tokens last ~60 days; emoji count by UTF-8 bytes. Use the Threads app ID/secret from the Threads use-case settings, and add your own Threads account as a tester and accept the invite before logging in.
- X: character count follows X's rules (a link counts 23, emoji and most non-Latin characters count 2). Pay-per-use (reported about $0.015/post, about $0.20 with a link). Tokens auto-refresh; the refresh token rotates and is re-saved; refreshes are serialised so they cannot race.
- Retries: read-only calls (GET/DELETE) retry up to 3 times on timeouts and 502/503/504. Publishing calls never retry, so a flaky network cannot create duplicate posts.
- `LINKEDIN_API_VERSION` default `202609` is listed as active in LinkedIn's docs as of Oct 2026; if rejected, `smoke_live.py` says so and tells you what to set.
