#!/usr/bin/env python3
"""Split a TECO (Tampa Electric) monthly bill between units that share one meter,
using per-circuit kWh from an Emporia Vue 3.

How the split works
-------------------
1. Bill totals come from the TECO PDF (best-effort parse) or the command line:
   service period, metered kWh, total amount due, and fixed charges.
2. kWh for each Vue circuit over the same service period comes from the Emporia
   cloud API (via PyEmVue) or from a CSV exported from the Emporia app.
3. Circuits are mapped to units in config.json. Usage on "shared" circuits, plus any
   metered kWh that no circuit accounts for ("unmonitored" = meter kWh minus the sum
   of the mapped circuits), is split by the configured method.
4. Fixed charges (customer charge etc.) are split by fixed_charge_split; everything
   else on the bill (energy, fuel, taxes, fees) is allocated by each unit's kWh share.
   TECO's tiered energy rate makes per-kWh pricing of each unit separately unfair, so
   the blended rate for the whole bill is applied to both units.
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from collections import defaultdict
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------- TECO bill

def parse_teco_pdf(path):
    """Pull service dates, kWh, amount due and the customer charge out of a TECO PDF.

    TECO changes its bill layout periodically, so every value can be overridden on
    the command line. Anything not found is returned as None.
    """
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    date = r"(\d{1,2}/\d{1,2}/\d{2,4})"
    money = r"\$?\s*([\d,]+\.\d{2})"
    found = {}

    m = re.search(rf"Service Period:?\s*{date}\s*(?:-|to|through)\s*{date}", text, re.I) \
        or re.search(rf"{date}\s*(?:-|to)\s*{date}", text)
    if m:
        found["start"], found["end"] = _parse_date(m.group(1)), _parse_date(m.group(2))

    m = re.search(r"(?:Total Used|Billing Period kWh|Energy Used|kWh Used)\D{0,20}([\d,]+)\s*kWh", text, re.I) \
        or re.search(r"([\d,]+)\s*kWh\b", text)
    if m:
        found["kwh"] = float(m.group(1).replace(",", ""))

    m = re.search(rf"(?:Total Amount Due|Amount Due|Total Due)\D{{0,20}}{money}", text, re.I)
    if m:
        found["total"] = float(m.group(1).replace(",", ""))

    m = re.search(rf"(?:Basic Service Charge|Customer Charge|Customer Facility Charge)\D{{0,20}}{money}", text, re.I)
    if m:
        found["fixed"] = float(m.group(1).replace(",", ""))

    return found


def _parse_date(s):
    fmt = "%m/%d/%Y" if len(s.split("/")[-1]) == 4 else "%m/%d/%y"
    return dt.datetime.strptime(s, fmt).date()


# ---------------------------------------------------------------- Emporia usage

def emporia_usage_api(cfg, start, end, tz):
    """Return {circuit name: kWh} for [start, end] (inclusive dates, local time)."""
    from pyemvue import PyEmVue
    from pyemvue.enums import Scale, Unit

    vue = PyEmVue()
    token_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".emporia_tokens.json")
    if os.path.exists(token_file):
        vue.login(token_storage_file=token_file)
    else:
        user = os.environ.get("EMPORIA_USER") or cfg.get("emporia", {}).get("username")
        password = os.environ.get("EMPORIA_PASSWORD") or cfg.get("emporia", {}).get("password")
        if not user or not password:
            sys.exit("Set EMPORIA_USER and EMPORIA_PASSWORD (or emporia.username/password in config).")
        vue.login(username=user, password=password, token_storage_file=token_file)

    begin = dt.datetime.combine(start, dt.time.min, tz).astimezone(dt.timezone.utc)
    finish = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tz).astimezone(dt.timezone.utc)

    usage = defaultdict(float)
    missing = defaultdict(int)
    for device in vue.get_devices():
        for channel in device.channels:
            name = _channel_name(device, channel)
            # Hourly data, fetched a week at a time, so the period edges land on
            # local midnight rather than on Emporia's UTC day buckets.
            cursor = begin
            while cursor < finish:
                chunk_end = min(cursor + dt.timedelta(days=7), finish)
                values, _ = vue.get_chart_usage(channel, cursor, chunk_end,
                                                scale=Scale.HOUR.value, unit=Unit.KWH.value)
                for v in values:
                    if v is None:
                        missing[name] += 1
                    else:
                        usage[name] += v
                cursor = chunk_end
    for name, hours in missing.items():
        print(f"warning: {name}: {hours} hour(s) of missing data in the period", file=sys.stderr)
    return dict(usage)


def _channel_name(device, channel):
    if channel.channel_num == "1,2,3":
        return "Mains" if not device.parent_device_gid else f"{device.device_name} Mains"
    return channel.name or f"{device.device_name} ch{channel.channel_num}"


def emporia_usage_csv(path, start, end):
    """Sum an Emporia app export (Time Bucket column + one '<name> (kWhs)' column per circuit)."""
    usage = defaultdict(float)
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        names = [re.sub(r"\s*\(kWhs?\)\s*$", "", h).strip() for h in header]
        for row in reader:
            if not row or not row[0].strip():
                continue
            day = _parse_csv_time(row[0]).date()
            if not (start <= day <= end):
                continue
            for name, value in zip(names[1:], row[1:]):
                if value.strip() and value.strip().lower() not in ("no cts", "none"):
                    usage[name] += float(value)
    return dict(usage)


def _parse_csv_time(s):
    s = s.strip()
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise ValueError(f"unrecognized timestamp in CSV: {s!r}")


# ---------------------------------------------------------------- allocation

def allocate(cfg, usage, bill):
    units = cfg["units"]
    unit_names = list(units)
    shared_split = cfg.get("shared_split", "proportional")
    fixed_split = cfg.get("fixed_charge_split", "equal")

    unit_kwh = {u: 0.0 for u in unit_names}
    assigned = set()
    for u, circuits in units.items():
        for c in circuits:
            if c not in usage:
                print(f"warning: circuit {c!r} (unit {u}) not found in Emporia data", file=sys.stderr)
            unit_kwh[u] += usage.get(c, 0.0)
            assigned.add(c)

    shared_kwh = 0.0
    for c in cfg.get("shared", []):
        if c not in usage:
            print(f"warning: shared circuit {c!r} not found in Emporia data", file=sys.stderr)
        shared_kwh += usage.get(c, 0.0)
        assigned.add(c)

    ignored = set(cfg.get("ignore", [])) | {"Mains"}
    unmapped = sorted(c for c in usage if c not in assigned and c not in ignored)
    for c in unmapped:
        print(f"warning: circuit {c!r} is not mapped to a unit; counted as unmonitored", file=sys.stderr)

    monitored = sum(unit_kwh.values()) + shared_kwh
    unmonitored = bill["kwh"] - monitored
    if unmonitored < 0:
        print(f"warning: circuits total {monitored:.1f} kWh, more than the meter's {bill['kwh']:.1f} kWh; "
              "scaling everything down to match the meter", file=sys.stderr)
    remainder = shared_kwh + unmonitored  # may be negative: then it trims both units

    direct_total = sum(unit_kwh.values())
    final_kwh = {}
    for u in unit_names:
        share = _share(shared_split, u, unit_names, unit_kwh, direct_total)
        final_kwh[u] = unit_kwh[u] + remainder * share

    variable = bill["total"] - bill["fixed"]
    rate = variable / bill["kwh"] if bill["kwh"] else 0.0
    rows = []
    for u in unit_names:
        fixed = bill["fixed"] * _share(fixed_split, u, unit_names, unit_kwh, direct_total)
        energy = final_kwh[u] * rate
        rows.append({
            "unit": u,
            "direct_kwh": unit_kwh[u],
            "allocated_kwh": final_kwh[u],
            "kwh_share": final_kwh[u] / bill["kwh"] if bill["kwh"] else 0.0,
            "fixed_charges": fixed,
            "usage_charges": energy,
            "amount_owed": fixed + energy,
        })
    summary = {
        "monitored_kwh": monitored,
        "shared_kwh": shared_kwh,
        "unmonitored_kwh": unmonitored,
        "mains_kwh": usage.get("Mains"),
        "blended_rate": rate,
        "unmapped": unmapped,
    }
    return rows, summary


def _share(method, unit, unit_names, unit_kwh, direct_total):
    if isinstance(method, dict):  # explicit fractions, e.g. {"Unit A": 0.6, "Unit B": 0.4}
        return float(method.get(unit, 0.0))
    if method == "proportional" and direct_total > 0:
        return unit_kwh[unit] / direct_total
    return 1.0 / len(unit_names)


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    p.add_argument("--bill-pdf", help="TECO bill PDF to read totals from")
    p.add_argument("--start", help="service period start, YYYY-MM-DD")
    p.add_argument("--end", help="service period end, YYYY-MM-DD")
    p.add_argument("--kwh", type=float, help="metered kWh on the bill")
    p.add_argument("--total", type=float, help="total amount due for this period's service ($)")
    p.add_argument("--fixed", type=float, help="fixed charges ($), e.g. the basic service charge")
    p.add_argument("--emporia-csv", help="use an Emporia app CSV export instead of the API")
    p.add_argument("--list-circuits", action="store_true", help="print circuit names and kWh, then exit")
    p.add_argument("--out", help="write the split to this CSV file")
    args = p.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)

    bill = parse_teco_pdf(args.bill_pdf) if args.bill_pdf else {}
    if args.start:
        bill["start"] = dt.date.fromisoformat(args.start)
    if args.end:
        bill["end"] = dt.date.fromisoformat(args.end)
    for key in ("kwh", "total", "fixed"):
        if getattr(args, key) is not None:
            bill[key] = getattr(args, key)
    bill.setdefault("fixed", cfg.get("default_fixed_charge", 0.0))

    if "start" not in bill or "end" not in bill:
        sys.exit("Need the service period: pass --start/--end or a --bill-pdf that has it.")

    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    if args.emporia_csv:
        usage = emporia_usage_csv(args.emporia_csv, bill["start"], bill["end"])
    else:
        usage = emporia_usage_api(cfg, bill["start"], bill["end"], tz)

    if args.list_circuits:
        for name, kwh in sorted(usage.items()):
            print(f"{kwh:10.2f} kWh  {name}")
        return

    missing = [k for k in ("kwh", "total") if k not in bill]
    if missing:
        sys.exit(f"Missing bill value(s) {missing}: pass them on the command line.")

    rows, s = allocate(cfg, usage, bill)

    print(f"TECO bill {bill['start']} to {bill['end']}: {bill['kwh']:.0f} kWh, "
          f"${bill['total']:.2f} total (${bill['fixed']:.2f} fixed)")
    if s["mains_kwh"] is not None and bill["kwh"]:
        diff = s["mains_kwh"] - bill["kwh"]
        print(f"Vue mains: {s['mains_kwh']:.1f} kWh ({diff:+.1f} vs. meter, {diff / bill['kwh']:+.1%})")
    print(f"Circuits on units: {s['monitored_kwh'] - s['shared_kwh']:.1f} kWh, shared: {s['shared_kwh']:.1f} kWh, "
          f"unmonitored: {s['unmonitored_kwh']:.1f} kWh, blended rate ${s['blended_rate']:.4f}/kWh\n")
    print(f"{'Unit':<16}{'Direct kWh':>12}{'Billed kWh':>12}{'Share':>8}{'Fixed':>10}{'Usage':>10}{'Owes':>10}")
    for r in rows:
        print(f"{r['unit']:<16}{r['direct_kwh']:>12.1f}{r['allocated_kwh']:>12.1f}{r['kwh_share']:>7.1%}"
              f"{r['fixed_charges']:>10.2f}{r['usage_charges']:>10.2f}{r['amount_owed']:>10.2f}")
    print(f"{'Total':<16}{'':>12}{sum(r['allocated_kwh'] for r in rows):>12.1f}{'':>8}"
          f"{sum(r['fixed_charges'] for r in rows):>10.2f}{sum(r['usage_charges'] for r in rows):>10.2f}"
          f"{sum(r['amount_owed'] for r in rows):>10.2f}")

    if args.out:
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["period_start", "period_end", *rows[0]])
            w.writeheader()
            for r in rows:
                w.writerow({"period_start": bill["start"], "period_end": bill["end"],
                            **{k: round(v, 4) if isinstance(v, float) else v for k, v in r.items()}})
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
