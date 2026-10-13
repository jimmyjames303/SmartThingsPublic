#!/usr/bin/env python3
"""ServiceMinder API smoke test: make one read call and print what comes back.

Usage:
    export SERVICEMINDER_API_KEY=...        # never commit the key
    python3 serviceminder/smoke_test.py

Optional:
    SERVICEMINDER_BASE_URL  (default https://www.serviceminder.com)
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = os.environ.get("SERVICEMINDER_BASE_URL", "https://www.serviceminder.com").rstrip("/")
API_KEY = os.environ.get("SERVICEMINDER_API_KEY")


def post(path, body):
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read().decode() or "{}")


def main():
    if not API_KEY:
        sys.exit("SERVICEMINDER_API_KEY is not set.")

    # Ask for a handful of contacts; ServiceMinder expects the key in the JSON body.
    body = {"ApiKey": API_KEY, "Skip": 0, "Limit": 5}
    try:
        status, data = post("/api/contacts/locate", body)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} from {BASE_URL}: {e.read().decode(errors='ignore')[:500]}")
    except urllib.error.URLError as e:
        sys.exit(f"Could not reach {BASE_URL}: {e.reason}")

    print(f"HTTP {status}  ResultCode={data.get('ResultCode')}  Message={data.get('Message')!r}")
    if data.get("ResultCode") != 0:
        sys.exit("API call did not succeed (ResultCode != 0).")

    # Show the shape of the data without dumping customer details.
    print("Top-level fields:", sorted(data.keys()))
    for key, value in data.items():
        if isinstance(value, list):
            print(f"{key}: {len(value)} records")
            if value and isinstance(value[0], dict):
                print("  record fields:", sorted(value[0].keys()))
    print("Connection OK.")


if __name__ == "__main__":
    main()
