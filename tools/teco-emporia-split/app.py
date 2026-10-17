"""Web front end for split_bill.py: upload a TECO bill PDF, get the split.

Runs as its own Railway service. Environment:
  APP_PASSWORD      required; HTTP basic auth password (user name: APP_USER, default "admin")
  EMPORIA_USER      Emporia account email
  EMPORIA_PASSWORD  Emporia account password
  SPLIT_CONFIG      the config JSON (same shape as config.example.json); falls back to config.json
"""
import base64
import csv
import datetime as dt
import hmac
import io
import json
import os
import time
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, Response, render_template, request

from split_bill import allocate, emporia_usage_api, parse_teco_pdf

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

HERE = os.path.dirname(os.path.abspath(__file__))
USAGE_CACHE_SECONDS = 3600
_usage_cache = {}  # (start, end) -> (fetched_at, usage, warnings)


def load_config():
    if os.environ.get("SPLIT_CONFIG"):
        return json.loads(os.environ["SPLIT_CONFIG"])
    with open(os.path.join(HERE, "config.json")) as f:
        return json.load(f)


def requires_auth(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        password = os.environ.get("APP_PASSWORD")
        if not password:
            return Response("APP_PASSWORD is not set on the server.", 503)
        auth = request.authorization
        user = os.environ.get("APP_USER", "admin")
        if not (auth and hmac.compare_digest(auth.username or "", user)
                and hmac.compare_digest(auth.password or "", password)):
            return Response("Login required.", 401, {"WWW-Authenticate": 'Basic realm="Bill split"'})
        return view(*args, **kwargs)
    return wrapper


def fetch_usage(cfg, start, end):
    """Emporia kWh per circuit, cached so re-running a period doesn't re-pull the API."""
    key = (start, end)
    hit = _usage_cache.get(key)
    if hit and time.time() - hit[0] < USAGE_CACHE_SECONDS:
        return hit[1], list(hit[2])
    warnings = []
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    usage = emporia_usage_api(cfg, start, end, tz, warn=warnings.append)
    _usage_cache[key] = (time.time(), usage, warnings)
    return usage, list(warnings)


def _form_value(name, cast):
    raw = (request.form.get(name) or "").strip().replace("$", "").replace(",", "")
    return cast(raw) if raw else None


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/")
@requires_auth
def index():
    return render_template("index.html", bill={}, result=None, errors=[])


@app.post("/")
@requires_auth
def split():
    errors = []
    bill = {}
    upload = request.files.get("bill_pdf")
    if upload and upload.filename:
        try:
            bill = parse_teco_pdf(io.BytesIO(upload.read()))
        except Exception as e:  # unreadable or non-PDF upload
            errors.append(f"Couldn't read that PDF: {e}")

    # Typed-in values win over whatever the PDF parse found.
    try:
        overrides = {
            "start": _form_value("start", dt.date.fromisoformat),
            "end": _form_value("end", dt.date.fromisoformat),
            "kwh": _form_value("kwh", float),
            "total": _form_value("total", float),
            "fixed": _form_value("fixed", float),
        }
    except ValueError as e:
        errors.append(f"Check the values you typed: {e}")
        overrides = {}
    bill.update({k: v for k, v in overrides.items() if v is not None})

    cfg = load_config()
    bill.setdefault("fixed", cfg.get("default_fixed_charge", 0.0))
    missing = [label for key, label in (("start", "service start"), ("end", "service end"),
                                        ("kwh", "kWh used"), ("total", "amount due"))
               if bill.get(key) is None]
    if missing:
        errors.append("Couldn't find " + ", ".join(missing) + " on the bill. Fill them in below.")
    elif bill["end"] < bill["start"]:
        errors.append("The service end date is before the start date.")
    if errors:
        return render_template("index.html", bill=bill, result=None, errors=errors), 400

    try:
        usage, warnings = fetch_usage(cfg, bill["start"], bill["end"])
    except Exception as e:
        errors.append(f"Couldn't get Emporia data: {e}")
        return render_template("index.html", bill=bill, result=None, errors=errors), 502

    rows, summary = allocate(cfg, usage, bill, warn=warnings.append)
    return render_template("index.html", bill=bill, errors=[], result={
        "rows": rows,
        "summary": summary,
        "warnings": warnings,
        "circuits": sorted(usage.items(), key=lambda kv: -kv[1]),
        "csv": _csv_data_uri(bill, rows),
        "filename": f"teco-split-{bill['start']}-to-{bill['end']}.csv",
    })


def _csv_data_uri(bill, rows):
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=["period_start", "period_end", *rows[0]])
    w.writeheader()
    for r in rows:
        w.writerow({"period_start": bill["start"], "period_end": bill["end"],
                    **{k: round(v, 4) if isinstance(v, float) else v for k, v in r.items()}})
    return "data:text/csv;base64," + base64.b64encode(out.getvalue().encode()).decode()


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
