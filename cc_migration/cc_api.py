"""Small CompanyCam Public API client shared by export.py and import.py.

Tokens are read from environment variables and never printed or logged.
API reference: https://developers.companycam.com (spec: /openapi/public_api_v1.yaml)
"""
import json
import os
import sys
import time
from urllib.parse import urlparse

import requests

API_BASE = os.environ.get("CC_API_BASE", "https://app.companycam.com/public_api/v1").rstrip("/")

# Published limits are per token: 240 GET/min, 100 POST/PUT/PATCH/DELETE/min (shared bucket).
# Pace slightly under them so 429s are rare; 429s are still handled below.
MIN_INTERVAL = {"GET": 60 / 220, "WRITE": 60 / 90}
MAX_RETRIES = 8
TIMEOUT = (15, 120)


class ApiError(Exception):
    def __init__(self, status, method, url, body):
        self.status = status
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:500]}")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def get_token(var):
    token = os.environ.get(var, "").strip()
    if not token:
        sys.exit(f"Environment variable {var} is not set.")
    return token


class Client:
    def __init__(self, token_var):
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {get_token(token_var)}",
            "Accept": "application/json",
        })
        self._last = {"GET": 0.0, "WRITE": 0.0}
        self._api_host = urlparse(API_BASE).netloc

    def _pace(self, method):
        bucket = "GET" if method == "GET" else "WRITE"
        wait = self._last[bucket] + MIN_INTERVAL[bucket] - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last[bucket] = time.monotonic()

    def request(self, method, path, params=None, json_body=None):
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        idempotent = method in ("GET", "PUT", "PATCH", "DELETE")
        for attempt in range(MAX_RETRIES + 1):
            self._pace(method)
            try:
                resp = self._session.request(method, url, params=params, json=json_body, timeout=TIMEOUT)
            except requests.ConnectionError as e:
                # A POST that failed to connect never reached the server, so it is safe to retry.
                # A read timeout on a POST may have been processed, so it is not retried.
                if attempt == MAX_RETRIES or (not idempotent and isinstance(e, requests.ReadTimeout)):
                    raise
                self._backoff(attempt, None, f"{method} {path}: {type(e).__name__}")
                continue
            except requests.Timeout:
                if attempt == MAX_RETRIES or not idempotent:
                    raise
                self._backoff(attempt, None, f"{method} {path}: timeout")
                continue

            retryable = resp.status_code == 429 or resp.status_code == 503 or (
                idempotent and resp.status_code >= 500)
            if retryable and attempt < MAX_RETRIES:
                self._backoff(attempt, resp.headers.get("Retry-After"), f"{method} {path}: HTTP {resp.status_code}")
                continue
            if resp.status_code >= 400:
                raise ApiError(resp.status_code, method, url, resp.text)
            if resp.status_code == 204 or not resp.content:
                return {}
            return resp.json()

    @staticmethod
    def _backoff(attempt, retry_after, why):
        try:
            delay = float(retry_after) if retry_after else min(60, 2 ** attempt)
        except ValueError:
            delay = min(60, 2 ** attempt)
        log(f"  retrying in {delay:.0f}s ({why})")
        time.sleep(delay)

    def get(self, path, **params):
        return self.request("GET", path, params=params or None).get("data")

    def post(self, path, body):
        return self.request("POST", path, json_body=body).get("data")

    def patch(self, path, body):
        return self.request("PATCH", path, json_body=body).get("data")

    def paginate(self, path, **params):
        """Yield every item from a cursor-paginated list endpoint."""
        params = {"limit": 100, **params}
        seen_cursors = set()
        while True:
            page = self.request("GET", path, params=params)
            data = page.get("data") or []
            yield from data
            meta = page.get("meta") or {}
            cursor = meta.get("next_cursor")
            if not meta.get("has_next") or not cursor or cursor in seen_cursors:
                return
            seen_cursors.add(cursor)
            params["after"] = cursor

    def download(self, url, dest):
        """Stream a file to dest via a .part file. Only sends the token to the API host itself."""
        headers = None if urlparse(url).netloc == self._api_host else {"Authorization": None}
        tmp = f"{dest}.part"
        for attempt in range(MAX_RETRIES + 1):
            try:
                with self._session.get(url, headers=headers, stream=True, timeout=TIMEOUT) as resp:
                    if resp.status_code == 429 or resp.status_code >= 500:
                        if attempt < MAX_RETRIES:
                            self._backoff(attempt, resp.headers.get("Retry-After"), f"download HTTP {resp.status_code}")
                            continue
                    if resp.status_code >= 400:
                        raise ApiError(resp.status_code, "GET", url.split("?")[0], resp.text[:200])
                    with open(tmp, "wb") as f:
                        for chunk in resp.iter_content(1 << 20):
                            f.write(chunk)
                    expected = resp.headers.get("Content-Length")
                    if expected and os.path.getsize(tmp) != int(expected):
                        raise requests.ConnectionError("incomplete download")
                os.replace(tmp, dest)
                return resp.headers.get("Content-Type", "")
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt == MAX_RETRIES:
                    raise
                self._backoff(attempt, None, f"download: {type(e).__name__}")

    def put_file(self, url, path, headers):
        """PUT a local file to a presigned upload URL (no CompanyCam auth header)."""
        for attempt in range(MAX_RETRIES + 1):
            try:
                with open(path, "rb") as f:
                    resp = requests.put(url, data=f, headers=headers, timeout=TIMEOUT)
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt == MAX_RETRIES:
                    raise
                self._backoff(attempt, None, f"upload: {type(e).__name__}")
                continue
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < MAX_RETRIES:
                self._backoff(attempt, resp.headers.get("Retry-After"), f"upload HTTP {resp.status_code}")
                continue
            if resp.status_code >= 400:
                raise ApiError(resp.status_code, "PUT", url.split("?")[0], resp.text)
            return


def write_json(path, data):
    """Write JSON atomically so an interrupted run never leaves a half-written file."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)
