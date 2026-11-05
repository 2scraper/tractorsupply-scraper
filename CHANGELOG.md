# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/) as closely as a CLI
toolkit can: a patch release means **fixes**, not that every flag and
default is frozen. A default that changes behaviour for an existing user is
said so at the top of its release notes.

## [Unreleased]

> **Behaviour change:** `stop_reason` no longer says `page_load_timeout`
> for a failure that was not a timeout. A closed page, a reset connection or
> a dead proxy is now `transport_error`, and a backend that kept answering
> `CircuitBreakerFallbackException` is `backend_unavailable`. Exit codes are
> unchanged.

### Fixed

- **Category pages in the site's new build resolve again.** The site now
  serves a category page in one of two builds, chosen per browser profile:
  the older one with `__NEXT_DATA__`, and an App Router one whose data sits
  in its React Server Components stream. The new build has no
  `__NEXT_DATA__`, so every `--category` run on it — the README's first
  command among them, and any search the site redirects to a category —
  stopped with exit 2 "names no id". The id is read from the stream now
  (24660 for poultry-feed-treats, the same id the old build states).
- **An unknown category in the new build is "no such category".** That
  build answers an unknown slug with HTTP 200 instead of 500, so it was
  reported as a changed layout. It is recognised by having no listing
  context in its payload; the words "not found" are no marker there, since
  the site's good pages carry them too.
- **A closed browser page is replaced instead of retried.** When the page
  under a run went away ("Target page, context or browser has been closed"),
  every retry went into the same dead page and the run ended with exit 5.
  The loop now opens a new page, and over `--cdp-endpoint` reconnects when
  the browser connection itself is gone.
- **A failed `--cdp-endpoint` connect is explained by what it was.** An
  HTTP 500 `proxy_error`/`proxy_timeout` (the service could not raise the
  profile's exit) was explained as "another run holds this pid" and given up
  on after about nine seconds; it is now retried with longer waits and said
  plainly. A connect that timed out was not retried at all, and printed the
  same pid explanation. `profile_locked` and an expired 401 keep their own
  messages.
- **A refusal over a Scraping Browser profile names the profile.** The retry
  said "from a fresh browser" while reusing the same profile and cookies; it
  now says so, and the advice is to use another `pid`.
- **The category page can be saved.** `--dump-html` now also writes the
  category page the run resolves its id from (`<dump>.category.html`), and a
  page with no id is saved to `<out>_category_debug.html` automatically, so
  the "open an issue" message has something to attach.

### Documentation

- The access table is re-measured (2026-11-05) beside the September one: on
  that day the search endpoint was refused through every client tried,
  including four Scraping Browser profiles, so no client obtained rows; a
  profile was refused after a handful of requests. The Selenium row states
  what was measured here and what an audit reported from a home connection.

## [0.2.0] — 2026-10-15

> **Behaviour change:** `--retries 0` is now a usage error (exit 2). It used
> to be accepted and made the page-load loop unreachable, so nothing was
> fetched and the engine reported the browser's own blank page as a result:
> exit 3 or 4 with a 39-byte "capture", in tens of milliseconds. If a
> script of yours passes 0, pass **1**: the flag counts ATTEMPTS, so 1 means
> "try once and do not retry".
>
> **Behaviour change:** a page the site served, whose own result count says
> it holds products, but that parses to zero rows is now a **parser
> failure** (`stop_reason: parser_found_nothing`): exit 5 on page 1, exit 6
> and `status: partial` on a later page. It used to be exit 4 ("the listing
> is empty") on page 1 and exit 0 `complete` on a later page, with that
> page's products silently missing.
>
> **Behaviour change:** `--proxy-block-retries` below 0, `--pages` and
> `--concurrency` below 1, `--page-size` outside 1-200, and a negative (or
> non-finite) `--delay` / `--retry-delay` are now usage errors (exit 2) in
> all three engines. `--proxy-block-retries -1` used to send no request at
> all and report exit 5; `--page-size 0` silently became 48.

### Fixed

- **A served page that parses to nothing is no longer "empty" or
  "complete".** When the response's own `resultsFound` puts rows on that
  page, or it carries product entries the parser could not read, the page
  fails with `parser_found_nothing` and is saved as a debug file. A page
  that its own total puts past the end (the listing shrank during the run)
  still ends the run as `end_of_listing`, complete.
- **`--fingerprint` on Selenium and pyppeteer now sets the fingerprint's
  user agent.** Both read it from `userAgent.value`, which neither response
  format has, so they installed the fingerprint's platform and WebGL strings
  over the browser's own UA. They now use the same reader as Playwright.
- **The numeric flags are validated before anything is fetched** (see the
  behaviour change above), with an error naming the flag.
- **A root-level `.html` / `.mhtml` dump is gitignored**, so `git add -A`
  cannot stage a `--dump-html page.html` capture.
- **Credential lifetime wording.** The README, TROUBLESHOOTING, the CDP 401
  hint and `.env.example` no longer repeat an unmeasured "about a day"; they
  state the measured case (an endpoint set 2026-09-28 still served by the
  scheduled canary on 2026-10-12).
- **Public files no longer point at private maintainer notes.** Section
  references in comments and docstrings were replaced by the rule itself,
  and the suite fails if such a reference returns.
- **`--retries 0` fetched nothing and called it a result.** The load loop is
  `range(1, retries + 1)`, so 0 never runs it and the engine judged
  `about:blank`. Three third-party audits of this family ran exactly that and
  reported a working scraper as broken. `--retries` now goes through
  `page_flow.attempts` in every entry point; a check reads the source of each
  one and calls the validator, so a new engine declaring the flag with a
  plain int fails the suite.

### Changed

- **The canary fails when its secret is missing.** Without
  `TRACTORSUPPLY_CDP_ENDPOINT` it used to go green with a notice; a canary
  that never reached the site has tested nothing, so it now fails with an
  error naming the secret. Exit 6 (incomplete) is reported by name.

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

### Changed

- **The canary runs weekly**, not only on dispatch. It was dispatch-only on
  the family's figure that a Scraping Browser endpoint's credentials "last
  about a day" — copied, never measured here. Measured: the endpoint set on
  2026-09-28 was still served on 2026-10-08 (a canary ran on it and
  passed, 144 of 270 and 144 of 1,070). Every place that repeated "about a
  day" now states the measured range, and an expired endpoint shows up as a
  red canary with a 401.

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
