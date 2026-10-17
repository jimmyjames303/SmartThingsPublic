# TECO + Emporia Vue bill splitter

Splits a Tampa Electric (TECO) monthly bill between two (or more) units that share
one meter. It uses per-circuit kWh from an Emporia Vue 3 for the bill's service period.

## Setup

```sh
pip install -r requirements.txt
cp config.example.json config.json   # then edit circuit names
export EMPORIA_USER=you@example.com EMPORIA_PASSWORD=...
python split_bill.py --start 2026-08-12 --end 2026-09-10 --list-circuits
```

`--list-circuits` prints the exact circuit names from your Vue. Copy them into
`config.json` under `units` (one list per unit) and `shared` (circuits both units
use, like exterior lights or a shared well pump). After the first login, tokens are cached in
`.emporia_tokens.json`. That file is gitignored, so you don't have to keep the password in the environment.

## Monthly run

```sh
# Read the totals from the TECO PDF (download it from the TECO account site)
python split_bill.py --bill-pdf teco_2026-09.pdf --out split_2026-09.csv

# Or type the numbers off the bill. These flags also override anything the PDF parse got wrong.
python split_bill.py --start 2026-08-12 --end 2026-09-10 \
    --kwh 1680 --total 245.37 --fixed 15.85 --out split_2026-09.csv

# Offline: use a CSV exported from the Emporia app (daily scale) instead of the API
python split_bill.py --emporia-csv export.csv --start ... --end ... --kwh ... --total ...
```

Use `--total` for the current period's charges only. Leave out any past-due balance
carried over from an earlier bill.

## Web app on Railway

`app.py` is a small password-protected web page. You upload the TECO PDF, it pulls
the Emporia data for that service period automatically, and it shows the split with
a CSV download. Run it as its own Railway service so it stays separate from your
other apps:

1. In Railway, create a **new project** (or a new service in an existing project) from
   this GitHub repo.
2. Service **Settings**:
   - **Root Directory**: `/tools/teco-emporia-split`
   - **Config file path**: `/tools/teco-emporia-split/railway.json`. Railway's config
     file doesn't follow the root directory, so give the full path. The `Procfile`
     has the same start command as a fallback.
3. **Variables**:

   | Variable | Value |
   |---|---|
   | `APP_PASSWORD` | Password for the page (the browser asks for it; user name `admin`, or set `APP_USER`) |
   | `EMPORIA_USER` / `EMPORIA_PASSWORD` | Your Emporia login |
   | `SPLIT_CONFIG` | The contents of your `config.json`, pasted as one JSON value |

   Keep the circuit mapping in `SPLIT_CONFIG`, not in a committed `config.json`, so it
   stays out of the repo.
4. **Networking → Generate Domain**, then open the URL.

The page won't load until `APP_PASSWORD` is set. Pulling a month of hourly Emporia
data takes a little while. Results are cached in memory for an hour, so
recalculating the same period with corrected bill numbers is quick.

Run it locally with `APP_PASSWORD=x python app.py` (uses `config.json`).

## How the money is divided

1. **Direct kWh**: the sum of each unit's own circuits over the service period.
2. **Shared + unmonitored kWh**: shared circuits, plus any meter kWh that no CT
   measured (meter kWh minus all mapped circuits). This pool is split by `shared_split`:
   `"proportional"` (default, by direct usage), `"equal"`, or explicit fractions such as
   `{"Unit A": 0.6, "Unit B": 0.4}`.
   The meter is always the source of truth, so the units' billed kWh add up exactly to the bill.
3. **Fixed charges** (`--fixed`, e.g. the basic service charge) are split by
   `fixed_charge_split` (default `"equal"`).
4. **Everything else** (energy, fuel, storm, taxes, franchise fee) is charged at the
   bill's blended $/kWh, based on each unit's billed kWh. TECO prices usage in tiers
   (kWh over 1,000 cost more), and the meter sees the combined usage. A blended rate
   shares that higher-tier cost fairly instead of putting it on whichever unit
   happens to be counted last.

The output also compares the Vue's mains reading with the meter. If they differ by
more than a few percent, check the CT orientation and the circuit multipliers in the
Emporia app before you trust the split.

## Notes

- The TECO PDF parse uses regexes and can break when TECO changes its bill layout.
  Check the printed totals and override them with flags if needed.
- Data is pulled hourly from local midnight on the start date through the end of the
  end date (`timezone` in the config). Meter reads aren't taken at midnight, so expect
  a small mismatch at the edges of each period.
- Any Vue circuit that isn't in `units`, `shared`, or `ignore` triggers a warning and
  is counted in the unmonitored pool.
