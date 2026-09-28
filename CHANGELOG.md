# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/) as closely as a CLI
toolkit can: a patch release means **fixes**, not that every flag and
default is frozen. A default that changes behaviour for an existing user is
said so at the top of its release notes.

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
  answers HTTP 500, is `missing`, not a fault to retry.
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
