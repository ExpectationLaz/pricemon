# pricemon

Checks competitor product pages once a day for a client. Each run stores the
prices and stock status, writes an Excel and CSV report, and sends the client a
digest of what changed by email and Telegram.

## Setup

```bash
py -3.13 -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and fill it in. Copy `settings.example.yaml` to
`settings.yaml` only if you want to change a default. Neither file is committed.

| `.env` value | What it is |
|---|---|
| `SMTP_HOST` | The mail provider's outgoing (SMTP) server, e.g. `smtp.example.com`. |
| `SMTP_PORT` | `587` (STARTTLS, the default) or `465` (SSL from the start). |
| `SMTP_USER` | The login for that mail account. |
| `SMTP_PASSWORD` | Its password. Many providers (Gmail, Outlook, Yahoo) need an "app password" you create in the account's security settings instead of the normal account password. |
| `SMTP_FROM` | The sender address shown on the emails. |
| `TELEGRAM_BOT_TOKEN` | The token of your Telegram bot (below). Keep it secret. |
| `OPERATOR_EMAIL` | Your own email address: you get the operator alerts here. |
| `OPERATOR_TELEGRAM_CHAT_ID` | Your own Telegram chat id: you get the operator alerts here. |

The server certificate of the mail server is always verified.

Telegram:

1. In Telegram, talk to `@BotFather`, send `/newbot` and follow the questions.
   It gives you the bot token.
2. Everyone who should receive messages (each client, and you) must open the
   bot in Telegram and press **Start**. A bot cannot message someone first.
3. To find a chat id, send the bot any message, then open
   `https://api.telegram.org/bot<token>/getUpdates` in a browser (replace
   `<token>` with the bot token) and read `chat` then `id` in the answer.
   That number is the `telegram_chat_id` of a client, or your
   `OPERATOR_TELEGRAM_CHAT_ID`.

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
| `page not found` | The URL is wrong or the product is gone. Check the URL. |
| `could not fetch` | A network error, a server error or a redirect to another site. Try again later; if the page needs JavaScript, try `--browser`. |
| `works with the browser fetcher` | The page works only with `--browser`. Create `sites/<domain>.yaml` with `fetcher: browser` (see Site rules), then check it again. |

If the price only appears after JavaScript runs, try `check-url <url> --browser`.
`--browser` is not a way around a `blocked` verdict: a site that blocks automated
requests is declined, with or without the browser. That needs the optional browser:

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
   each site that needs one. Open the same page in a browser and compare the
   price: a site's own structured data can be wrong (a real shop showed
   £31.90 on the page and £22 in its structured data). If they differ, write a
   site rules file with `skip_structured_data: true` and a `price` selector.
   Do not base a stock rule on text that is only hidden by CSS, such as an
   "Out of stock" label every page carries.
4. Prove that delivery works, before the first real alert:
   `.venv/Scripts/pricemon.exe test-notify clients/<client>.yaml`
   It sends a short test message through every channel: the client's email and
   Telegram, then your operator email and Telegram. One line per channel:
   `... : sent` (check that it arrived), `... : FAILED: <reason>` (fix `.env` or
   the address, see below) or `... : not configured`. It exits with 1 if any
   channel failed or if no channel is configured at all.
5. Do a first run without sending anything:
   `.venv/Scripts/pricemon.exe run clients/<client>.yaml --dry-run`
   Open `data/<client>/reports/<date>-dry-run.xlsx` and check the Problems
   sheet. Real runs ignore everything a dry run stored, so you can repeat a
   dry run at any time without affecting what the client is told.
6. Schedule it (below).

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

One Windows Task Scheduler task per client. Run these commands in Command
Prompt (`cmd.exe`), not PowerShell: the quoting differs. Replace the path and
client name:

```bat
schtasks /Create /TN "pricemon-acme-shop" /SC DAILY /ST 07:00 /TR "\"C:\path\to\price-monitor\run-client.cmd\" acme-shop"
```

After creating the task, open it in Task Scheduler and check three settings:

- Conditions tab: on a laptop, clear "Start the task only if the computer is on
  AC power".
- Settings tab: tick "Run task as soon as possible after a scheduled start is
  missed".
- General tab: choose whether it runs only when you are logged on, or whether
  it runs whether you are logged on or not.

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
| `pricemon test-notify <client.yaml>` | Send a test message through every channel (client and operator email and Telegram). Exits 1 if one failed or none is configured. |
| `pricemon accept <client.yaml> <url>` | Accept a price that was held for verification (below). It becomes the new baseline and the client is told in the next run's digest. |

`run` exits with:

- 0: the run completed, even if some pages failed.
- 1: invalid config or a fatal error.
- 2: the run completed, but a client alert could not be delivered. It is
  retried in the next run.

## What gets sent

- **Client:** one digest per run and channel when something changed, with the
  Excel report attached. Set `send_when_no_changes: true` to send it daily. The
  digest also says how many pages could not be checked that day. An alert that
  could not be delivered (mail server down, wrong chat id) is not lost: it is
  included in the next run's digest, marked as a change that could not be
  delivered earlier.
- **You:** an alert when a page has failed `operator_failure_threshold` runs in
  a row (default 3), repeated every `operator_failure_threshold` runs while it
  keeps failing, and an alert when a price moved by `verify_threshold_pct` or
  more (default 70%). Such a jump usually means the site changed its layout,
  so the client is not alerted until you have checked it. The page is held at
  its last trusted price, and you hear about it again in every run until you
  resolve it. You also get an alert when a client alert could not be delivered.

## When you get an operator alert

The alert lists one line per problem. Each line starts with a keyword.

- `FAILING <url>`: the page failed several runs in a row. Run
  `pricemon check-url <url>` on it. Then fix the site rules in
  `sites/<domain>.yaml`, fix the URL in the client config, or tell the client
  the site is no longer supported.
- `VERIFY <product>`: the price moved by `verify_threshold_pct` or more. Open the
  page in a browser. If the new price is real, run
  `pricemon accept <client.yaml> <url>`; the client is told in the next run's
  digest. If `accept` answers `Nothing to accept`, the page's most recent run
  did not hold a price (for example the page failed to load in that run): run
  `check-url` on the page and wait for the next run that holds it. If the price shown is wrong, the site changed its layout: fix
  `sites/<domain>.yaml` and confirm with `check-url`. Until one of those happens
  the page is held at its last trusted price and the alert repeats.
- `DELIVERY <problem>`: the client's email or Telegram could not be reached. Run
  `pricemon test-notify <client.yaml>`, then fix `.env` or the client's address.
  The alert is sent again automatically in the next run.

The log of every run is in `data\<client>-run.log`.

## Limits

- Public product pages only: no logins and no personal data.
- `robots.txt` is respected. If a site's `robots.txt` cannot be read, that
  site is skipped for the run.
- Bot protection is never bypassed. A blocked site is reported as unsupported.
- A redirect to another site is not followed: the page is reported as failed
  ("redirected to another site"). Redirects within the same site are followed.
  A page that the browser fetcher is redirected away from to another site is
  reported as a failure, the same as with the normal fetcher.
- Check a site's terms of use before taking a job that targets it.
