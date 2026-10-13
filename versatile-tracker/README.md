# Versatile order → shipment tracker

Monitors your [versatile.net](https://www.versatile.net/) (Versatile
High-Performance Coatings) orders, pulls the tracking numbers off each order,
checks the carrier (FedEx or Southeastern Freight Lines), and emails you when a
shipment's status changes.

versatile.net runs on NetSuite SuiteCommerce, so order data is read from the
account's standard JSON services after a normal customer login.

## Status

Being built incrementally:

1. **`versatile_client.py`** — logs in and reads order history. *(done — needs a
   real login to confirm the order/tracking data shape)*
2. Carrier lookups (FedEx Track API + SEFL) — *next, once we see a real order*
3. Change detection + email + scheduling — *after that*

## Credentials (never put these in chat or in the repo)

Set these as **environment secrets** on the cloud environment
(title-bar environment menu → Edit → Network secrets / environment variables):

| Variable             | What it is                                  |
| -------------------- | ------------------------------------------- |
| `VERSATILE_EMAIL`    | your versatile.net account email            |
| `VERSATILE_PASSWORD` | your versatile.net account password         |

FedEx and SEFL API credentials will be added here when we reach that step.

## Run the order reader

```bash
cd versatile-tracker
python3 versatile_client.py
```

It prints your order history as JSON so we can see exactly where the FedEx
tracking numbers and SEFL PRO numbers live.
