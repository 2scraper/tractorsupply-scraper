# Tractor Supply Scraper — Store Prices, Stock and Ratings in Python

[![release](https://img.shields.io/github/v/release/2scraper/tractorsupply-scraper)](https://github.com/2scraper/tractorsupply-scraper/releases)
[![tests](https://github.com/2scraper/tractorsupply-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/tractorsupply-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/tractorsupply-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/tractorsupply-scraper/actions/workflows/canary.yml)
![python](https://img.shields.io/badge/python-3.9%20%7C%203.12-blue)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
![engines](https://img.shields.io/badge/engines-playwright%20%7C%20selenium%20%7C%20pyppeteer-lightgrey)
![needs](https://img.shields.io/badge/needs-Scraping%20Browser%20API%20(US)-orange)

**Tractor Supply scraper** — an open-source (MIT) Python tool that reads tractorsupply.com categories, whole departments and keyword searches and writes products priced at a specific store, with unit price, stock, pickup availability and ratings, to JSON or CSV. On this site it needs the 2Captcha Scraping Browser API with a US exit: measured 2026-09-28, that was the only client served, while datacentre curl, local Chromium, a US residential proxy and the 2Captcha Scraper API were all refused. Use it to build a product dataset or for store-level price monitoring, comparing two runs priced at the same store with `diff_runs.py`.

Scrapes [tractorsupply.com](https://www.tractorsupply.com) listings — a
category, a whole department, or a keyword search — into JSON and CSV, with
the prices **of a specific store**:

| `--mode` | what | example |
|---|---|---|
| `category` | a category or a whole department | `--category poultry-feed-treats`, `--url …/tsc/category/pet` |
| `search` | a keyword search, in any of the site's eight orderings | `--search "dog food" --sort price-asc` |

One row per product, 35 columns: price and price range, list price and the
discount computed from it, unit price ("$0.33 per lb"), minimum-advertised-
price handling, rating and review count, stock, ship and pickup availability
at the chosen store, brand, model number, badges, the first promotion, and
the number of variants. Every run writes a `<out>.meta.json` beside the
output with the site's own total, so a file says "144 of 272" rather than
only "144", and with the stores the prices are for.

---

## Start with the part most scrapers bury

**On this site you need the 2Captcha Scraping Browser API, with a US exit —
and on 2026-11-05 even that did not reach the product data.** See the second
table: access here changes, and the dates matter.

Measured 2026-09-28. Akamai Bot Manager fronts the whole site:

| client | answer |
|---|---|
| plain curl, any User-Agent, datacentre (Hetzner, Helsinki) | HTTP 403 "Access Denied" — on every URL, `robots.txt` included |
| local Chromium, headless and headful, same address | 403 |
| US residential proxy (2Captcha, Comcast exit), curl | connection reset |
| US residential proxy, headless Chromium | `ERR_HTTP2_PROTOCOL_ERROR` |
| US residential proxy, headful Chromium | homepage 200; every `/tsc/` navigation and `fetch()` 403 (13 of 13) |
| 2Captcha Scraper API (its own exits) | target HTTP 403 on every URL tried, homepage included (4 of 4) |
| **Scraping Browser, `country-us`** | **homepage 200; every request after it 200** (2026-09-28) |

Re-measured 2026-11-05, from the same kind of datacentre address and through
a US residential proxy exit (a Verizon address):

| client | answer |
|---|---|
| plain curl, datacentre | HTTP 403 "Access Denied" (unchanged) |
| local Chromium, headless, datacentre | refused on the homepage, exit 3 (unchanged) |
| US residential proxy, headful Chromium | category page refused, and the search endpoint refused (exit 3) |
| Scraping Browser, `country-us`, four profiles | homepage 200 on all four; the store lookup and a category page served on most; **the search endpoint (`catalogSearch`) refused on all four** |

So on that day no client here obtained product rows. Two more things the
re-measurement showed:

- **A profile is refused after a handful of requests**, and a retry inside
  it does not help: the profile's cookies are what is refused. The run says
  so and suggests another `pid`; other profiles were served where a flagged
  one was not.
- **The site serves category pages in two builds** — the older one carrying
  `__NEXT_DATA__`, and a newer App Router one carrying the same data in its
  React Server Components stream — and which one depends on the browser
  profile, not on the address. The scraper reads both.

A third-party audit (November 2026) reported a home residential connection
on which the Selenium engine *was* served (a search of 87 rows, a category
of 48). It was not reproduced here: the residential proxy exit above was
refused.

Even through the Scraping Browser, *navigating* to a category page is
refused. What is let through is what the site's own front end does: load the
homepage, then call the site's endpoints with `fetch()`. So that is exactly
what every engine here does — one navigation, then every request as a
same-origin `fetch()` from that page.

It is also where the data is. A category page's HTML carries the category's
id and **no products**: the grid is painted in the browser from a search
endpoint (`/gtwy/SiteSearch/catalogSearch`, newline-delimited JSON). This
repo reads that endpoint directly.

Live, through one `country-us` profile, all complete:

| engine | listing | rows | time |
|---|---|---|---|
| Playwright | category `poultry-feed-treats`, 3 pages | 144 of 272 | 11 s |
| pyppeteer | search "dog food", price ascending, 3 pages | 144 of 1,076 | 8 s |
| Playwright | search "dog food", ZIP 75001, 2 pages of 200 | 400 of 1,076 | — |
| Playwright | department `pet`, 2 pages | 96 of 14,678 | — |
| Selenium | local Chrome | exit 3, `blocked_akamai` (see Engines) | — |

---

## Install

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt -r requirements-playwright.txt
```

No `playwright install` is needed over `--cdp-endpoint`: the browser is
remote. Install **one** engine per virtualenv; the three libraries pin
versions of their dependencies that cannot all be satisfied at once.

Put the endpoint in `.env` (see `.env.example`), never on a command line:

```bash
cp .env.example .env
# TRACTORSUPPLY_CDP_ENDPOINT=ws://{login}-zone-scraping_browser-country-us-pid-{profileId}:{password}@cb.2captcha.com:9222
python3 env_config.py      # what was picked up, without printing secrets
```

## Run

```bash
# a category, three pages of 48
./venv/bin/python playwright_scraper.py --category poultry-feed-treats --pages 3

# a whole department
./venv/bin/python playwright_scraper.py --url https://www.tractorsupply.com/tsc/category/pet --pages 5

# a search, cheapest first, priced at the stores near Dallas
./venv/bin/python playwright_scraper.py --search "dog food" --sort price-asc --zip 75001

# fewer requests: up to 200 a page
./venv/bin/python playwright_scraper.py --category dog-food --page-size 200 --pages 10
```

`--pages` is planned against the total the site states on page 1, so asking
for more pages than exist fetches all of them. `--sort` takes the site's own
orderings: `popular` (default), `rating`, `name-asc`, `name-desc`,
`price-asc`, `price-desc`, `recency`, `new`.

Outputs `tractorsupply_products.json`, `.csv` and `.meta.json`
(`--out` changes the prefix). A real one is committed as
[`sample_output.json`](sample_output.json) / [`.csv`](sample_output.csv).

**Reading the output safely.** Each file is replaced atomically (written
beside its target, then renamed), so a crash or a full disk mid-run leaves
the previous good file whole rather than truncated. Three files cannot be
replaced at once, though, so the sidecar is written last and lists the
others with their size and sha256 (`outputs`): read the sidecar first, check
the files against it, and treat a mismatch as "mid-update, read again". The
sidecar also says how complete the run is against the site's own count:
`total_results`, `duplicates_dropped`, and — only when the run read every
page of the listing — `rows_missing_vs_total`. Two full live runs measured
0 (270 of 270, 36 of 36); a non-zero means the listing moved while it was
being read. In the CSV only, a text cell that begins with `=`, `+`, `-` or
`@` is prefixed with an apostrophe so a spreadsheet does not execute it; the
JSON keeps the site's bytes, and `csv_cells_escaped` says how many.

---

## Seven things about Tractor Supply that will look like bugs

### 1. Prices are per store

Every request names the stores near a ZIP code, and the site prices at
them. On one 37-product category, a Georgia store set and a Texas one agreed
on 36 prices and differed on one (1,799.99 against 1,899.99). So a run's
store is part of its question: `--zip` picks it (default **37027**,
Brentwood TN, where the company is headquartered — a fixed choice, so two
runs from two exits price the same catalogue at the same store), and
`store_id` / `zip_code` are in every row and the sidecar. `diff_runs.py`
refuses to compare two runs priced at different stores.

### 2. `price` is the low end of a range

A product with sizes has one row and several prices. The endpoint's own
field called `price` is one variant's, and on most of the 53 of 249 products
where it differed from the low end it was the **highest**. So `price` is the
low end, `price_max` the high end, and they are equal on a single-SKU
product. `original_price` is filled only where both it and the price are
single values: on a range, a "was" figure does not belong to the price
beside it.

### 3. `in_stock` is empty on multi-variant products

The site's inventory stream lists no multi-variant product at all — 0 of
108 measured — so their `in_stock`, `ship_available` and `pickup_available`
are null rather than guessed. `variant_count` says which rows those are.
`sold_via` ("Online Only", "In Stores Only") is the sales channel the site
states, which is not stock.

### 4. A search the site cannot match returns products anyway

"qzxqzxvvv" came back as 100 pairs of Wrangler jeans, and nothing in the
response says so. The run measures how many rows of page 1 mention the
query, warns below 20%, and records the share as `query_relevance` in the
sidecar.

### 5. A department's visible id returns nothing

`/tsc/category/farm-ranch` names itself `1001811`, and the search endpoint
answers that id with zero results. The id it filters on (26654) is the
parent each of the department's categories names, and that is what the run
uses. A department listing is every product in it: Pet was 14,678.

### 6. A category that does not exist is HTTP 500

Not 404: an unknown slug is answered with the site's own error page under a
server-error status. The run recognises the page, stops with exit 2 and says
there is no such category, instead of retrying a "fault".

### 7. Some searches are not searches

"chicken feed" is answered with **0 results** and a redirect to
`/tsc/catalog/poultry-feed` — the site's own front end then navigates there.
Read as a search, that is "the listing is empty" about a query with
hundreds of products, which is what v0.1.0 reported (exit 4). The run now
follows a redirect to a category, reads that category, and records
`searched_keyword` and `search_redirected_to` in the sidecar; the rows are
then `mode: category`. A redirect to anything else stops with exit 2 and
says where it pointed, rather than calling the catalogue empty.

---

## Engines

| engine | status on this site |
|---|---|
| `playwright_scraper.py` | primary. Served over `--cdp-endpoint`. |
| `puppeteer_scraper.py` | served over `--cdp-endpoint`. pyppeteer is effectively unmaintained; here for parity. |
| `selenium_scraper.py` | chromedriver cannot send the credentials a Scraping Browser endpoint carries, so the Scraping Browser is out of its reach. A local Chrome was refused from every address measured here (2026-09-28 and 2026-11-05); an audit reported it served from a home connection, not reproduced here. A refusal is reported correctly (exit 3). |

All three share one fetch loop (`page_flow.run_pages`), so they agree on
exit codes, run status and every retry decision by construction. Each
engine is only driver plumbing and one piece of JavaScript, the `fetch()`,
written in its driver's dialect.

`--concurrency N` runs N workers, each with its own browser and exit; it is
ignored with `--cdp-endpoint`, because a Scraping Browser profile takes one
live connection. Use several `pid`s, one run each.

---

## What the 2Captcha products buy, and when

* **Scraping Browser API** — the one client this site served, from a US
  exit. `country-us`. Its credentials expire, and how long they last was measured here rather than assumed: the endpoint this repo was built with, set on 2026-09-28, was still served by the scheduled canary on 2026-10-12, 14 days later. A 401 on connect is the sign that one has expired.
* **Proxies** — supported (`--proxy`, `--proxy-file`, rotation and
  per-exit retries), but on 2026-09-28 a US residential exit was **not**
  enough for local Chromium (the table above). Measure before paying for
  volume.
* **Fingerprints** — supported (`--fingerprint`), for local browsers. Not
  measured to change Akamai's answer here.
* **Captcha solving** — this repo does not implement a solver, and the
  family's `--captcha-api`, `--solve-captcha` and `--min-score` flags are
  absent on purpose: none of the refusals the site served (19 captured)
  carried a captcha widget, only Akamai's plain Access Denied. A flag that
  configures nothing looks configurable and is not.
* **Scraper API** — this repo does not implement a Scraper API path: its
  own exits were refused on every URL tried, homepage included.

---

## Exit codes

| code | meaning |
|---|---|
| 0 | ok |
| 1 | crash (a bug — please report it) |
| 2 | bad usage, or a listing the site does not have: an unknown category, a ZIP with no store |
| 3 | blocked: Akamai refused the client (`stop_reason: blocked_akamai`) |
| 4 | the listing is genuinely empty; nothing is written, so an earlier good file survives |
| 5 | the data never arrived: a timeout, a remote-browser error (401 = expired endpoint), a reset connection, or the endpoint refused the parameters and said why. A reset is also how Akamai refused local Chromium in testing, and the error says so; it stays 5 rather than 3 because a reset alone does not prove a refusal |
| 6 | partial: some pages came back, a later one did not; the sidecar lists which |

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for each.

## Configuration

Precedence, highest first: a flag you typed → an exported environment
variable → `.env` → the default. `.env` holds `TWOCAPTCHA_KEY`,
`TRACTORSUPPLY_CDP_ENDPOINT`, `TRACTORSUPPLY_PROXY` and `TRACTORSUPPLY_URL`
(a default listing, which a typed `--category` or `--search` overrides).
`--page-size` is capped at 200 because the endpoint refuses more with
HTTP 400.

## Comparing two runs

```bash
python3 diff_runs.py --old monday.json --new tuesday.json
```

Added, removed and changed products by `sku`. It refuses runs that are not
both complete, and runs of different listings, orderings or store sets, since
every line would then describe the question rather than the site.
`removed` means "no longer in the slice fetched", not "discontinued", unless
both runs fetched every page.

## Tests

```bash
python3 smoke_test.py
```

Offline, about a second, no browser and no key. The fixtures are real
responses captured 2026-09-28, cut down by `make_fixtures.py`, which proves
each one parses identically to its original. CI runs the suite on Python 3.9
and 3.12, once more per engine in its own virtualenv, and builds the Docker
image. The canary (`canary.yml`) runs three real pages of each mode weekly
through the Scraping Browser; without a `TRACTORSUPPLY_CDP_ENDPOINT` secret
it fails with an error naming that secret, because a GitHub runner is one
of the clients the site refuses and a canary that never reached the site
has tested nothing. When the endpoint expires it goes red with a 401.

## Legal

This reads public catalogue pages the way the site's own front end does, as
an anonymous visitor. Whether and how you may scrape a site depends on your
jurisdiction and its terms; that is your responsibility as the operator.
Nothing here logs in, adds to a cart or places an order.

## Guides

- [How to run the 2scraper scrapers: setup, proxies, exit codes and access modes](https://2captcha.com/h/how-to-run-2scraper-open-source-scrapers) — installation, `.env`, `.meta.json` for the whole family.

## Related scrapers

Same CLI conventions, same output contract (one row per item, JSON or CSV, a `.meta.json` sidecar and shared exit codes):

- [Woolworths scraper](https://github.com/2scraper/woolworths-scraper) — Prices, unit prices, specials, health star ratings…
- [Amazon scraper](https://github.com/2scraper/amazon-scraper) — Search results, best-seller grids, product pages and reviews…
- [Andie Swim scraper](https://github.com/2scraper/andieswim-scraper) — Collections, keyword lookups and per-size stock…
- [Bershka scraper](https://github.com/2scraper/bershka-scraper) — Every SKU in a Bershka category
- [Catawiki scraper](https://github.com/2scraper/catawiki-scraper) — Auction lots, bids, reserves, estimates and sellers…
- [Etsy scraper](https://github.com/2scraper/etsy-scraper) — Search, category, shop and listing pages as JSON or CSV…

All scrapers: [github.com/2scraper](https://github.com/2scraper) · Need another site? [Open a request](https://github.com/2scraper/.github/issues/new?title=%5BScraper%20request%5D%20)
