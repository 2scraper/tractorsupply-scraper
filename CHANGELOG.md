# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/) as closely as a CLI
toolkit can: a patch release means **fixes**, not that every flag and
default is frozen. A default that changes behaviour for an existing user is
said so at the top of its release notes.

## [0.1.1] — 2026-10-08

> **Behaviour change for existing users:** a keyword the site redirects to a
> category (for example "chicken feed") used to end with exit 4, "the
> listing is empty". It now reads that category and exits 0, with
> `searched_keyword` and `search_redirected_to` in the sidecar and the rows
> marked `mode: category`. A pipeline that branched on exit 4 for such a
> keyword was branching on a false statement about the catalogue.

From a third-party audit (2026-10-08), each finding reproduced before it
was fixed.

### Fixed

- **A failed rewrite no longer destroys the previous good output.** JSON,
  CSV and the sidecar were opened with truncation, so a crash or a full disk
  during a rerun left the last good files as a few bytes of invalid JSON
  (reproduced: 4,518 bytes became 13). Each is now written beside its
  target and renamed over it, keeping the mode `open()` would have given a
  new file and an existing file's own mode. Lifted from woolworths-scraper.
- **A search the site redirects is followed, not called empty** (above).
- **A reset landing explains itself.** Without `--cdp-endpoint` the run
  warns up front, and a homepage that fails with `ERR_HTTP2_PROTOCOL_ERROR`
  or a reset connection says that this is how Akamai refused local Chromium
  in testing. Still exit 5: a reset alone does not prove a refusal.

### Added

- `outputs` in the sidecar: each output file with its size and sha256, so a
  consumer can tell a complete set from one caught mid-update.
- `duplicates_dropped`, and, when a run read every page,
  `rows_missing_vs_total` against the site's own count.
- CSV formula neutralisation (`csv_cells_escaped`), CSV only. It fires on
  0 of 18,611 string cells in 1,049 unique live rows (2026-10-08); it is here because every string in
  a row was written by someone else.

## [0.1.0] — 2026-09-28

First release. Category, department and search listings from
tractorsupply.com's own search endpoint, priced per store, over three
browser engines that share one fetch loop.

### Added

- `--mode category` (`--category SLUG`, or a `/tsc/catalog/…` or
  `/tsc/category/…` URL): a category or a whole department. The category
  page is fetched once for its id; a department's id is the parent its
  categories name, because the id it shows returns nothing.
- `--mode search` (`--search KEYWORD`, or a `/tsc/search/…` URL), with a
  warning and a sidecar `query_relevance` for the site's habit of answering
  an unmatched query with unrelated products.
- `--zip`: the stores the prices and stock are for (default 37027). Store
  and ZIP are in every row and the sidecar; `diff_runs.py` refuses to
  compare two store sets.
- `--sort` with the site's eight orderings, and `--page-size` up to the
  endpoint's own 200.
- A 35-column `Product` row: the family's sixteen-column prefix, then page,
  position, mode, sort, item SKU, model number, the price range, unit price,
  MAP handling, badge, promotion, variant count, buyability, sales channel,
  ship and pickup availability, store and ZIP.
- Every request is a same-origin `fetch()` from the homepage, the one route
  Akamai let through, and only through the Scraping Browser API with a US
  exit. The README's access table has the measurements.
- A refusal is recognised in both of its encodings (entity-escaped in raw
  bytes, plain in a browser's DOM), and without a status (Selenium has
  none). A wrong pricing zone, which the endpoint answers with an exception
  under HTTP 200, is `rejected`, not content. An unknown category, which
  answers HTTP 500, is `missing`, not a fault to retry. A backend failure
  reported the same way as a refused parameter (`catalog.error:
  CircuitBreakerFallbackException`, met by the first canary on page 3 of a
  search) is `unavailable` and retried, not reported as your parameters
  being wrong.
- pyppeteer answers a proxy's auth challenge through CDP's `Fetch` domain:
  its own `page.authenticate()` depends on a method current Chromium lacks.

### Not included, on purpose

- **No captcha path.** None of the 19 refusals captured carried a widget;
  the family's `--captcha-api`, `--solve-captcha` and `--min-score` flags are
  absent rather than inert, and a test keeps them absent.
- **No Scraper API client.** Its exits were refused on every URL tried,
  homepage included.
- **No product-detail mode.** A product page's own data carries no price
  (it is filled in per store in the browser), so this repo does not
  implement one yet.
- **Selenium is not served on this site**: chromedriver cannot use a
  credentialled endpoint, and local Chrome is refused. The engine is kept
  for parity and reports the refusal correctly.
