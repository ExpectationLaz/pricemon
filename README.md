# pricemon

Checks competitor product pages once a day for a client. Each run stores the
prices and stock status, writes an Excel and CSV report, and sends the client a
digest of what changed by email and Telegram.

## Setup

```bash
py -3.13 -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and fill in the SMTP account, the Telegram bot
token and your own operator contacts. Copy `settings.example.yaml` to
`settings.yaml` only if you want to change a default. Neither file is committed.

Run the tests with `.venv/Scripts/python.exe -m pytest -q`.

## Quoting a job: will this site work?

```bash
.venv/Scripts/pricemon.exe check-url https://shop.example/product/123
```

The verdict line tells you which case you are in:

| Verdict | Meaning |
|---|---|
| `works` | The site publishes structured data. No extra work. |
| `needs a site rules file` | Write `sites/<domain>.yaml` (below), about 15 minutes per site. |
| `unsupported` | The site blocks automated requests or disallows the page. Decline it. |

If the price only appears after JavaScript runs, try `check-url <url> --browser`.
That needs the optional browser:

```bash
.venv/Scripts/python.exe -m pip install -e ".[browser]"
.venv/Scripts/playwright.exe install chromium
```

## Adding a client

1. Copy `clients/example.yaml` to `clients/<client>.yaml` and fill in the
   products and competitor URLs. The client name may use lowercase letters,
   digits and hyphens.
2. Check it: `.venv/Scripts/pricemon.exe validate clients/<client>.yaml`
3. Run `check-url` on one URL per competitor site. Write a site rules file for
   each site that needs one.
4. Do a first run without sending anything:
   `.venv/Scripts/pricemon.exe run clients/<client>.yaml --dry-run`
   Open `data/<client>/reports/<date>-dry-run.xlsx` and check the Problems
   sheet. Real runs ignore everything a dry run stored, so you can repeat a
   dry run at any time without affecting what the client is told.
5. Schedule it (below).

The first real run is a baseline: it records prices and sends no alerts.
Alerts start with the second real run.

## Site rules

`sites/<anything>.yaml`, one file per hostname. `www.` is ignored;
`shop.example.com` and `example.com` are different sites.

```yaml
domain: competitor-b.example
fetcher: http                  # or browser
min_interval_seconds: 5        # optional
skip_structured_data: false    # true if the site's own structured data is wrong
title:    { css: "h1.product-title" }
price:    { css: "span.price" }             # add attr: "content" to read an attribute
currency: EUR                               # or { css: "...", attr: "..." }
decimal_separator: ","                      # optional
stock:
  css: ".availability"
  in_stock_text: ["In stock"]
  out_of_stock_text: ["Sold out"]
```

Find the selectors with the browser's "Inspect" tool, then confirm with
`check-url`.

The price selector must match text that holds exactly one number. If the
element also contains a struck-through old price, or shows the cents in a
separate element, `check-url` reports that the price text has more than one
number. Use a narrower selector, or read the price from an attribute with
`attr`. A selector that is not valid CSS is rejected when the config loads.

## Scheduling

One Windows Task Scheduler task per client. Replace the path and client name:

```bat
schtasks /Create /TN "pricemon-acme-shop" /SC DAILY /ST 07:00 /TR "\"C:\path\to\price-monitor\run-client.cmd\" acme-shop"
```

Each run appends its summary to `data\<client>-run.log`. To remove the task:

```bat
schtasks /Delete /TN "pricemon-acme-shop"
```

The PC must be on at the scheduled time.

## Commands

| Command | What it does |
|---|---|
| `pricemon run <client.yaml> [--dry-run]` | Check every URL, store results, write the report, send digests. `--dry-run` sends nothing. |
| `pricemon check-url <url> [--browser]` | Test one page without storing anything. |
| `pricemon report <client.yaml> [--date YYYY-MM-DD] [--dry-run]` | Rebuild a day's report from stored data. Uses the last real run of that date; `--dry-run` uses the last dry run. |
| `pricemon validate <client.yaml>` | Check a config without fetching. |

`run` exits with 0 when the run completed, even if some pages failed, and with
1 for an invalid config or a fatal error.

## What gets sent

- **Client:** one digest per run and channel when something changed, with the
  Excel report attached. Set `send_when_no_changes: true` to send it daily.
- **You:** an alert when a page has failed `operator_failure_threshold` runs in
  a row (default 3), repeated every `operator_failure_threshold` runs while it
  keeps failing, and an alert when a price moved by `verify_threshold_pct` or
  more (default 70%). Such a jump usually means the site changed its layout,
  so the client is not alerted until you have checked it.

## Limits

- Public product pages only: no logins and no personal data.
- `robots.txt` is respected. If a site's `robots.txt` cannot be read, that
  site is skipped for the run.
- Bot protection is never bypassed. A blocked site is reported as unsupported.
- Check a site's terms of use before taking a job that targets it.
