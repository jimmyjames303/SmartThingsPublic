#!/usr/bin/env python3
"""Read-only health check for the CompanyCam API v2.

Uses the bearer token in the COMPANYCAM_DEST_TOKEN environment variable and
issues GET requests only, so nothing in the account is created or changed:

    GET /company
    GET /users/current
    GET /projects
    GET /projects/{first_project_id}/photos
    GET /tags
    GET /users

Usage:
    COMPANYCAM_DEST_TOKEN=... python3 scripts/companycam_check.py [--verbose]

Exit status is 0 when every call succeeds, 1 if any call fails, and 2 if the
token is missing.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://api.companycam.com/v2"
TOKEN_ENV = "COMPANYCAM_DEST_TOKEN"
TIMEOUT = 30
PAGE_SIZE = 5


def api_get(token, path, params=None):
    """Perform a GET and return (status, parsed_body, elapsed_seconds, error)."""
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "User-Agent": "companycam-check/1.0",
        },
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, _parse(body), time.monotonic() - start, "HTTP %d %s" % (e.code, e.reason)
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return None, None, time.monotonic() - start, "request failed: %s" % reason
    return status, _parse(raw), time.monotonic() - start, None


def _parse(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _name(user):
    full = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p)
    return full or user.get("email_address") or str(user.get("id"))


def summarize_company(data):
    return "%s (id %s)" % (data.get("name"), data.get("id"))


def summarize_current_user(data):
    return "%s <%s>, role %s" % (_name(data), data.get("email_address"), data.get("user_role"))


def summarize_projects(data):
    if not data:
        return "no projects returned"
    names = ", ".join("%s (id %s)" % (p.get("name"), p.get("id")) for p in data[:3])
    return "%d returned on first page: %s%s" % (len(data), names, ", ..." if len(data) > 3 else "")


def summarize_photos(data):
    if not data:
        return "no photos in this project"
    first = data[0]
    return "%d returned on first page; first photo id %s, captured_at %s" % (
        len(data), first.get("id"), first.get("captured_at"))


def summarize_tags(data):
    if not data:
        return "no tags returned"
    labels = ", ".join(str(t.get("display_value") or t.get("value")) for t in data[:5])
    return "%d returned on first page: %s%s" % (len(data), labels, ", ..." if len(data) > 5 else "")


def summarize_users(data):
    if not data:
        return "no users returned"
    names = ", ".join(_name(u) for u in data[:5])
    return "%d returned on first page: %s%s" % (len(data), names, ", ..." if len(data) > 5 else "")


def summarize(fn, data):
    if isinstance(data, list) and fn in (summarize_company, summarize_current_user):
        data = data[0] if data else {}
    if fn in (summarize_company, summarize_current_user) and not isinstance(data, dict):
        return "unexpected response shape: %r" % (data,)
    if fn not in (summarize_company, summarize_current_user) and not isinstance(data, list):
        return "unexpected response shape: %r" % (data,)
    return fn(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print the full JSON body of each response")
    args = parser.parse_args()

    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        print("error: %s is not set" % TOKEN_ENV, file=sys.stderr)
        return 2

    paged = {"per_page": PAGE_SIZE, "page": 1}
    checks = [
        ("GET /company", "/company", None, summarize_company),
        ("GET /users/current", "/users/current", None, summarize_current_user),
        ("GET /projects", "/projects", paged, summarize_projects),
        # The photos check is filled in once the first project id is known.
        ("GET /projects/{id}/photos", None, paged, summarize_photos),
        ("GET /tags", "/tags", paged, summarize_tags),
        ("GET /users", "/users", paged, summarize_users),
    ]

    results = []
    first_project_id = None
    for label, path, params, fn in checks:
        if fn is summarize_photos:
            if first_project_id is None:
                results.append((label, "SKIP", None, 0.0, "no project available to read photos from"))
                continue
            path = "/projects/%s/photos" % first_project_id
            label = "GET %s" % path

        status, data, elapsed, error = api_get(token, path, params)
        if error:
            detail = error
            if isinstance(data, dict):
                msg = data.get("errors") or data.get("error") or data.get("message")
                if msg:
                    detail += ": %s" % msg
            results.append((label, "FAIL", status, elapsed, detail))
        else:
            results.append((label, "OK", status, elapsed, summarize(fn, data)))
            if fn is summarize_projects and isinstance(data, list) and data:
                first_project_id = data[0].get("id")

        if args.verbose and data is not None:
            print("----- %s -----" % label)
            print(json.dumps(data, indent=2) if not isinstance(data, str) else data)
            print()

    print("CompanyCam API v2 check (%s)" % BASE_URL)
    print()
    width = max(len(r[0]) for r in results)
    for label, outcome, status, elapsed, detail in results:
        code = str(status) if status is not None else "-"
        print("%-4s  %-*s  %3s  %5.2fs  %s" % (outcome, width, label, code, elapsed, detail))

    failed = sum(1 for r in results if r[1] == "FAIL")
    skipped = sum(1 for r in results if r[1] == "SKIP")
    print()
    print("%d passed, %d failed, %d skipped" % (len(results) - failed - skipped, failed, skipped))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
