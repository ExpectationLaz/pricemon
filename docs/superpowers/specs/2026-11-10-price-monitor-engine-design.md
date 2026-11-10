# Price Monitor Engine: Design

Date: 2026-11-10
Status: approved design, not yet implemented

## 1. Purpose

A reusable engine behind a productized service: small online shops pay to have
competitor product pages checked once a day. Each client gets a daily
spreadsheet of competitor prices and stock, plus an alert when a competitor
changes a price or goes in or out of stock.

The engine is built once. Adding a client must be a configuration job of one to
two hours, not a development job.

### Success criteria

- A new client whose competitors publish structured product data is onboarded
  by writing one YAML file, with no code changes.
- A competitor site without structured data is supported by adding one site
  rules file, with no code changes.
- One failing page never stops a run or hides the results of the other pages.
- The operator (the person running the service) hears about broken pages
  before the client does.

## 2. Scope

### In version 1

- Daily monitoring of public product pages listed by the client
- Extraction of title, price, currency and stock status
- Price and stock history per client
- Change detection and alerts by email and Telegram
- Daily Excel and CSV report
- Command line interface, scheduled with Windows Task Scheduler

### Not in version 1

- Web dashboard, client logins, self-serve signup
- Google Sheets output
- Proxies or any technique for getting past bot protection
- Automatic matching of a client's products to competitor pages (the client
  supplies the URLs)
- Pages behind a login

## 3. Architecture

Data moves in one direction through a single run:

```
client config -> fetch -> extract -> store -> detect changes -> report + notify
```

Each stage is a separate module with one job. Stages pass plain data objects
and do not call each other sideways; `runner.py` is the only module that knows
the order.

### Project layout

```
price-monitor/
  pyproject.toml
  README.md
  .env.example
  settings.example.yaml
  src/pricemon/
    cli.py            command line entry point
    runner.py         orchestrates one run for one client
    config.py         loads and validates client config, site rules, settings
    models.py         Observation, Event, RunSummary
    fetch.py          HTTP fetcher, optional browser fetcher, block detection
    extract/
      __init__.py     the extraction chain
      jsonld.py       schema.org Product from JSON-LD
      meta.py         Open Graph / product meta tags
      rules.py        per-site CSS rules
      price.py        price text -> Decimal
    store.py          SQLite access
    detect.py         change detection
    report.py         Excel and CSV output
    notify/
      __init__.py     digest building, channel dispatch
      email.py        SMTP sender
      telegram.py     Telegram Bot API sender
  sites/              one rules file per competitor domain (committed)
  clients/            one config per client (not committed, except example.yaml)
  data/               databases and reports (not committed)
  tests/
    fixtures/         saved product pages
```

### Libraries

- Python 3.13
- httpx (HTTP), selectolax (HTML parsing), PyYAML and pydantic (config),
  openpyxl (Excel), python-dotenv (secrets)
- argparse from the standard library for the CLI
- pytest for tests
- Playwright as an optional extra, `pricemon[browser]`

## 4. Configuration

### 4.1 Client config: `clients/<client>.yaml`

```yaml
client: acme-shop              # used for folder names; lowercase, digits, hyphens
products:
  - name: "Bosch GSR 12V drill"
    sku: "BOS-123"             # optional, the client's own reference
    own_price: 129.00          # optional, shown in the report for comparison
    urls:
      - https://competitor-a.example/p/123
      - https://competitor-b.example/drill
alerts:
  events: [price_down, price_up, out_of_stock, back_in_stock]
  min_price_change_pct: 1.0    # smaller changes are stored but not alerted
  verify_threshold_pct: 70     # larger changes are marked "verify", not alerted
notify:
  email: [owner@acme.example]  # optional
  telegram_chat_id: "123456"   # optional
  send_when_no_changes: false  # true sends the daily report regardless
report:
  formats: [xlsx, csv]
```

Validation rules: `client` and at least one product with at least one URL are
required. A URL may appear only once per client. Unknown keys are an error, so
typos are caught at load time.

### 4.2 Site rules: `sites/<domain>.yaml`

Needed only when structured data is missing or wrong for a domain.

```yaml
domain: competitor-b.example
fetcher: http                  # http (default) or browser
min_interval_seconds: 5        # optional override of the global rate limit
skip_structured_data: false    # true if the site's structured data is wrong
title:    { css: "h1.product-title" }
price:    { css: "span.price", attr: null }   # attr reads an attribute instead of text
currency: EUR                  # fixed value, or { css: "...", attr: "..." }
decimal_separator: ","         # optional, overrides automatic detection
stock:
  css: ".availability"
  in_stock_text: ["In stock", "Available"]
  out_of_stock_text: ["Sold out", "Out of stock"]
```

A site rules file is matched to a URL by exact hostname after removing a
leading `www.`. `shop.example.com` and `example.com` are different sites and
need separate files.

### 4.3 Global settings: `settings.yaml`

```yaml
user_agent: "pricemon/0.1"
min_interval_seconds: 5        # minimum gap between requests to one domain
timeout_seconds: 20
retries: 2
respect_robots_txt: true
operator_failure_threshold: 3  # consecutive failed runs before the operator is alerted
```

### 4.4 Secrets: `.env`

`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`,
`TELEGRAM_BOT_TOKEN`, `OPERATOR_EMAIL`, `OPERATOR_TELEGRAM_CHAT_ID`.

`.env`, `settings.yaml`, `clients/*` (except `clients/example.yaml`) and
`data/` are in `.gitignore`. A channel whose secrets are missing is skipped
with a warning in the run summary; it is not a fatal error.

## 5. Components

### 5.1 Fetcher (`fetch.py`)

Input: a URL and the site rules for its domain. Output: a `FetchResult` with
`status`, `http_status`, `html` and `error`.

- Requests to one domain are at least `min_interval_seconds` apart. URLs are
  processed round-robin across domains so the waiting overlaps. Requests are
  sequential; version 1 has no concurrency.
- Network errors and 5xx responses are retried `retries` times with a doubling
  delay starting at 2 seconds. 4xx responses are not retried.
- `robots.txt` is fetched once per domain per run. A disallowed URL gets status
  `disallowed` and is not fetched.
- Block detection: HTTP 403 or 429, or a response body that is a known
  challenge page (CAPTCHA or "checking your browser" markers), gives status
  `blocked`. The engine never retries around a block, solves a challenge, or
  rotates identity.
- `fetcher: browser` loads the page in headless Chromium through Playwright
  and returns the rendered HTML. If Playwright is not installed the result is
  `fetch_error` with a message that says how to install it.

Fetch statuses: `ok`, `fetch_error`, `blocked`, `disallowed`, `not_found`
(HTTP 404 or 410).

### 5.2 Extractor (`extract/`)

Input: HTML, URL, site rules. Output: `title`, `price`, `currency`, `in_stock`,
`method`, or an extraction error.

The chain runs in this order and stops at the first step that yields a price:

1. `jsonld`: a schema.org `Product` in a JSON-LD block, including one nested in
   `@graph`. Price and currency come from `offers` (an `Offer`, a list of
   offers, or an `AggregateOffer`, where `lowPrice` is used). Stock comes from
   `offers.availability`.
2. `meta`: `product:price:amount` and `product:price:currency` meta tags, with
   `og:title` for the title and `product:availability` for stock.
3. `rules`: the CSS rules from the site rules file.

Steps 1 and 2 are skipped when the site rules set `skip_structured_data: true`.
Fields missing from the winning step are filled from later steps where
possible (for example price from JSON-LD and stock from site rules).

`in_stock` is `true`, `false` or `null` (unknown). Unknown is a valid result.
`currency` is a three-letter code or `null` when no step provides one; a
missing currency is not an error. A page with no price from any step is an
`extract_error`.

Price parsing (`price.py`) turns text such as `1.299,00 €`, `$1,299.00` or
`1 299` into a `Decimal`:

- Currency symbols, letters and spaces are removed.
- If both `.` and `,` appear, the last one is the decimal separator.
- If only one of them appears and it appears more than once (`1,299,000`), it
  is a thousands separator.
- If only one of them appears and it appears once, it is a thousands separator
  when exactly three digits follow it (`1.299` is 1299) and the decimal
  separator otherwise (`12.50`, `12,5`).
- `decimal_separator` in the site rules overrides this detection.
- A result that is zero, negative or unparsable is an extraction error.

### 5.3 Store (`store.py`)

One SQLite file per client at `data/<client>/pricemon.db`. Prices are stored as
decimal text, never as floats. Times are UTC ISO 8601.

```
runs(id, started_at, finished_at, urls_total, urls_ok, urls_failed, dry_run)

observations(id, run_id, url, product_name, fetched_at, status, http_status,
             title, price, currency, in_stock, method, error)
  index on (url, fetched_at)

events(id, run_id, observation_id, url, product_name, type,
       old_price, new_price, change_pct, verify, alerted)
```

Every configured URL writes one observation per run, including failures. An
observation's `status` is one of the fetch statuses from 5.1 or
`extract_error`; `ok` means the page was fetched and a price was extracted. A
"failed run" for a URL is a run whose observation for it is not `ok`. The
store exposes: start and finish a run, add an observation, get the last `ok`
observation for a URL, count consecutive failed runs for a URL, add an event,
and list a run's observations and events.

### 5.4 Change detector (`detect.py`)

A pure function: `(previous ok observation, new ok observation, alert rules)`
gives a list of events. It does no I/O.

- No previous observation: no events. The first run is a baseline.
- Price changed: `price_down` or `price_up` with `change_pct`. Compared only
  when the currency is the same (two missing currencies count as the same); a
  currency change produces no price event and is listed under problems in the
  report.
- `in_stock` true to false: `out_of_stock`. False to true: `back_in_stock`.
  Any transition involving unknown produces no event.
- `verify` is true when the absolute `change_pct` is at least
  `verify_threshold_pct`.
- An event is alerted when its type is in `alerts.events`, `verify` is false,
  and for price events the absolute change is at least `min_price_change_pct`.
  Events that are not alerted are still stored and still appear in the report.

### 5.5 Reporter (`report.py`)

Writes `data/<client>/reports/<YYYY-MM-DD>.xlsx` and, if configured, a CSV of
the Prices sheet with the same base name. The date is the local date of the
run. A second run on the same day overwrites that day's files.

- **Prices** sheet, one row per URL: product, SKU, own price, competitor
  domain, title, price, currency, in stock, previous price, change %, last
  checked, status, URL.
- **Changes** sheet, one row per event in this run, with a "verify" column.
- **Problems** sheet, one row per URL whose latest status is not `ok`, with the
  reason and the number of consecutive failed runs.

A URL that failed this run shows its last known price on the Prices sheet with
its status set to the failure, so the client always sees the latest good value.

### 5.6 Notifier (`notify/`)

One digest per run per channel, never one message per event.

- Client digest: a count line, then one line per alerted event ("Bosch GSR 12V
  drill at competitor-a.example: 129.00 to 119.00 EUR, down 7.8%"). Email
  attaches the Excel report; Telegram sends the text followed by the file.
- A client digest is sent only when there is at least one alerted event, unless
  `send_when_no_changes` is true.
- Operator digest: sent to `OPERATOR_EMAIL` and `OPERATOR_TELEGRAM_CHAT_ID`
  when any URL reaches `operator_failure_threshold` consecutive failed runs, or
  when a run has events marked `verify`. It lists each URL with its status and
  error. A URL triggers the operator digest once when it crosses the threshold
  and not again until it has recovered and failed again.
- A sending failure is logged and recorded in the run summary. It does not
  fail the run, and the report file is still written.

### 5.7 Runner and CLI (`runner.py`, `cli.py`)

```
pricemon run <client.yaml> [--dry-run]
pricemon check-url <url> [--browser]
pricemon report <client.yaml> [--date YYYY-MM-DD]
pricemon validate <client.yaml>
```

- `run`: load config, start a run, fetch and extract every URL, store
  observations, detect and store events, write the report, send digests, print
  a summary. `--dry-run` does all of that except sending digests, and marks the
  run as a dry run.
- `check-url`: fetch and extract one URL without touching any database, then
  print each field, which chain step produced it, and the fetch status. This is
  the tool for quoting a job: it shows in a minute whether a prospect's URLs
  work out of the box, need a site rules file, or are unsupported.
- `report`: rebuild the report for a date from stored data, with no fetching.
- `validate`: load and validate a client config and the site rules it needs,
  and report which domains have no rules file.

Exit codes: 0 when the run completed, even if some pages failed; 1 for an
invalid config or a fatal error.

## 6. Scheduling

One Windows Task Scheduler task per client runs `pricemon run` once a day. The
README documents the exact command to register a task. There is no long-running
process. Moving to a server later means the same command under cron.

## 7. Error handling summary

| Situation | Behaviour |
|---|---|
| One page fails to fetch or extract | Recorded as a failed observation; run continues |
| Site blocks the request | Status `blocked`; no circumvention; shown under Problems |
| `robots.txt` disallows the URL | Status `disallowed`; not fetched |
| Page fails several runs in a row | Operator digest at the configured threshold |
| Price change at or above the verify threshold | Stored, marked "verify", not sent to the client, sent to the operator |
| Notification channel fails or is unconfigured | Logged in the run summary; run and report unaffected |
| Invalid client config | Run does not start; exit code 1 with the validation message |

## 8. Legal and ethical limits

- Public product pages only. No logins, no personal data.
- `robots.txt` is respected by default.
- Bot protection is never bypassed. Blocked sites are reported to the operator
  as unsupported, and the operator declines or refunds that part of the job.
- Requests are rate limited per domain.
- The operator is responsible for checking a target site's terms before taking
  a job.

## 9. Testing

- **Extractor**: saved HTML pages in `tests/fixtures/`, covering JSON-LD (single
  offer, offer list, aggregate offer, `@graph`), meta tags, site rules, and
  pages with no price. No network.
- **Price parsing**: a table of input strings and expected decimals, including
  both European and US formats and invalid input.
- **Change detector**: made-up observation pairs covering every event type, the
  baseline case, unknown stock, currency change, and both thresholds.
- **Fetcher**: httpx `MockTransport` for retries, 403/429/404 handling,
  challenge-page detection, robots handling and rate limiting (with a fake
  clock).
- **Store**: a temporary SQLite file.
- **Notifier**: fake senders that capture messages; digest wording and the
  operator "once per failure streak" rule.
- **Runner**: an end-to-end test with mocked HTTP and fake senders over two
  runs, checking the database, the report sheets and the digests.

## 10. Build order

1. Project skeleton, config loading and validation
2. Price parsing and the extraction chain, with `check-url`
3. Fetcher
4. Store
5. Change detector
6. Runner and `run --dry-run`
7. Reporter
8. Notifier (email, then Telegram) and operator alerts
9. Browser fetcher (optional extra)
10. README with setup, onboarding a client and scheduling
