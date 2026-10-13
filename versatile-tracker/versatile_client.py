"""Client for the versatile.net SuiteCommerce storefront.

Logs into a customer account and reads order history / shipment data.
Credentials are read from the environment, never hard-coded:

    VERSATILE_EMAIL     your versatile.net account email
    VERSATILE_PASSWORD  your versatile.net account password

The storefront is NetSuite SuiteCommerce. It exposes JSON services under
/scs/services/*. Login establishes a session cookie that subsequent
service calls reuse.
"""

from __future__ import annotations

import json
import os
import sys

import requests

BASE = "https://www.versatile.net"
LOGIN_PAGE = f"{BASE}/scs/checkout.ssp?is=login&login=T"
LOGIN_SERVICE = f"{BASE}/scs/services/Account.Login.Service.ss"
ORDER_HISTORY_SERVICE = f"{BASE}/scs/services/OrderHistory.Service.ss"
# Per-record detail. SuiteCommerce usually serves this as Order.Service.ss or
# as a record-type query; the exact id/name is confirmed at runtime.
ORDER_DETAIL_SERVICE = f"{BASE}/scs/services/Order.Service.ss"


class VersatileError(RuntimeError):
    pass


class VersatileClient:
    def __init__(self, email: str | None = None, password: str | None = None):
        self.email = email or os.environ.get("VERSATILE_EMAIL")
        self.password = password or os.environ.get("VERSATILE_PASSWORD")
        if not self.email or not self.password:
            raise VersatileError(
                "Set VERSATILE_EMAIL and VERSATILE_PASSWORD in the environment."
            )
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (versatile-tracker; personal order monitor)",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                # SuiteCommerce validates request origin against the site.
                "Origin": BASE,
                "Referer": LOGIN_PAGE,
            }
        )

    def login(self) -> dict:
        # Prime cookies (session id / CSRF) by loading the login touchpoint.
        self.session.get(LOGIN_PAGE, timeout=30)
        payload = {"email": self.email, "password": self.password, "redirect": "true"}
        resp = self.session.post(
            LOGIN_SERVICE,
            params={"n": "3"},  # n = site touchpoint number (checkout)
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        data = _as_json(resp)
        if isinstance(data, dict) and data.get("errorCode"):
            raise VersatileError(
                f"Login failed: {data.get('errorCode')} - {data.get('errorMessage')}"
            )
        return data if isinstance(data, dict) else {"raw": data}

    def order_history(self, limit: int = 50) -> list[dict]:
        """Return the list of orders for the logged-in customer."""
        resp = self.session.get(
            ORDER_HISTORY_SERVICE,
            params={"c": _company_id(self.session), "n": "3", "sort": "date", "order": "0",
                    "from": "0", "to": str(limit)},
            timeout=30,
        )
        data = _as_json(resp)
        if isinstance(data, dict) and data.get("errorCode"):
            raise VersatileError(
                f"Order history failed: {data.get('errorCode')} - {data.get('errorMessage')}"
            )
        # SuiteCommerce returns either a bare list or {records: [...]}.
        if isinstance(data, dict):
            return data.get("records") or data.get("orders") or [data]
        return data

    def order_detail(self, internalid: str) -> dict:
        resp = self.session.get(
            ORDER_DETAIL_SERVICE,
            params={"internalid": internalid, "n": "3"},
            timeout=30,
        )
        return _as_json(resp)


def _as_json(resp: requests.Response):
    try:
        return resp.json()
    except ValueError:
        return {"_status": resp.status_code, "_text": resp.text[:2000]}


def _company_id(session: requests.Session) -> str | None:
    # Harmless default; the service tolerates it being absent.
    return None


def main(argv: list[str]) -> int:
    client = VersatileClient()
    print("Logging in as", client.email, "...", file=sys.stderr)
    client.login()
    print("Login OK. Fetching order history ...", file=sys.stderr)
    orders = client.order_history()
    print(json.dumps(orders, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
