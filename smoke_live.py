"""Read-only live check. Run it right after `python oauth.py ...` to prove each connection works.

    python smoke_live.py            # LinkedIn, Instagram, Facebook Page, Threads
    python smoke_live.py --x        # also X (X charges per API call, so it is opt-in)

It NEVER posts, replies or deletes. Exit code 0 only if every configured platform passed.
Platforms with no token are skipped, not failed.
"""
import re
import sys

import envstore
import server


def _check(name, fn, ok=lambda r: True, hint=""):
    try:
        result = fn()
        if not ok(result):
            return name, "FAIL", f"unexpected reply: {str(result)[:150]}. {hint}".strip()
        return name, "PASS", ""
    except Exception as e:  # report every failure, keep going
        return name, "FAIL", f"{str(e)[:300]} {hint}".strip()


def _linkedin_version_accepted() -> dict:
    """Asks LinkedIn about a post that is not ours. 403/404 means 'version accepted, you may not see it'
    (fine); a version complaint means our LinkedIn-Version header is no longer valid."""
    try:
        server._request("GET", f"{server._li()}/rest/posts/urn%3Ali%3Ashare%3A1", headers=server._li_headers())
    except RuntimeError as e:
        msg = str(e)
        if re.search(r"NONEXISTENT_VERSION|is not active|-> 426", msg):
            raise RuntimeError("LinkedIn rejected the API version. Set LINKEDIN_API_VERSION in .env to a newer YYYYMM. " + msg)
        if re.search(r"-> (403|404)", msg):
            return {"version": "accepted", "detail": msg[:80]}
        raise
    return {"version": "accepted"}


def run(include_x: bool = False) -> list:
    g = envstore.get
    rows = []

    def section(label, configured, checks):
        if not configured:
            rows.append((label, "SKIP", "not connected (run oauth.py)"))
            return
        rows.extend(_check(f"{label}: {n}", f, ok, h) for n, f, ok, h in checks)

    section("LinkedIn", g("LINKEDIN_ACCESS_TOKEN"), [
        ("token works (profile)", server.linkedin_whoami, lambda r: "sub" in r,
         "Token may be expired (LinkedIn tokens last ~60 days): rerun `python oauth.py linkedin`."),
        ("API version accepted", _linkedin_version_accepted, lambda r: True, ""),
    ])
    section("Instagram", g("INSTAGRAM_ACCESS_TOKEN") and g("INSTAGRAM_BUSINESS_ID"), [
        ("account info", server.instagram_account_info, lambda r: "username" in r,
         "Needs a Business/Creator account; rerun `python oauth.py meta`."),
        ("publishing quota", server.instagram_publishing_limit, lambda r: "data" in r, ""),
        ("recent media", lambda: server.instagram_recent_media(3), lambda r: "data" in r, ""),
    ])
    section("Facebook Page", g("FACEBOOK_PAGE_TOKEN") and g("FACEBOOK_PAGE_ID"), [
        ("recent posts", lambda: server.facebook_page_recent_posts(1), lambda r: "data" in r,
         "Needs the pages_read_engagement permission."),
    ])
    section("Threads", g("THREADS_ACCESS_TOKEN") and g("THREADS_USER_ID"), [
        ("recent posts", lambda: server.threads_recent_posts(1), lambda r: "data" in r, ""),
    ])
    if include_x:
        section("X", g("X_ACCESS_TOKEN"), [
            ("token works (whoami)", server.x_whoami, lambda r: "data" in r,
             "If this says payment/credits, add credit in the X developer console."),
        ])
    else:
        rows.append(("X", "SKIP", "pass --x to check (X charges per call)"))
    return rows


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    rows = run("--x" in argv)
    width = max(len(r[0]) for r in rows)
    for name, status, detail in rows:
        print(f"{status:<5} {name:<{width}}  {detail}")
    failed = [r for r in rows if r[1] == "FAIL"]
    passed = [r for r in rows if r[1] == "PASS"]
    print(f"\n{len(passed)} passed, {len(failed)} failed, {len(rows) - len(passed) - len(failed)} skipped")
    if not passed and not failed:
        print("Nothing is connected yet. Run `python oauth.py <linkedin|meta|threads|x>` first.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
