# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`requests`, so any import of `playwright_scraper`, `puppeteer_scraper` or
`selenium_scraper` in a test has to sit inside `try/except ImportError` with
the skip recorded. This is easy to get wrong locally, where you almost
certainly have an engine installed and an unguarded import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but two things are
**not** masked: raw response dumps and your shell history. Before pasting any
output into an issue or a PR, replace keys, proxy passwords and full
`ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

Tractor Supply changing its site is the normal way this stops working, and it
has its own issue template. Every row comes out of ONE endpoint — the search
the site's own listing page calls from a web worker,
`/gtwy/SiteSearch/catalogSearch` — and a category page is fetched only for
its id. So there are four things that can break, and each is loud or
guarded:

1. **The request.** The endpoint wants the front end's headers (`channel`,
   `zoneid`, `x-api-version`) and a store list. A request it refuses comes
   back as `errorCode`/`errorMessage` under HTTP 400, or — for a wrong zone —
   as a `catalog` record carrying a Java exception under HTTP 200. Both are
   classified `rejected`: the run stops naming the site's own complaint, and
   is neither retried nor counted as blocked.
2. **The category id.** Read from the category page's `__NEXT_DATA__`
   (`catIdDetails`), or, on a department page, from the parent every child
   names. If neither is there the run stops with exit 2 and says so, rather
   than searching an id that returns nothing.
3. **A product's own field names** (`partNumber`, `offerPriceMin`,
   `displayPrice.list_price_min`, …). This is the one that can be QUIET: the
   row still writes, with that column null. `page_flow.CORE_FIELDS` is the
   guard, a coverage floor of 99% on the columns every captured product
   carried.
4. **Akamai.** It already refuses every client this repo tried but one. If
   the Scraping Browser stops being served, the canary says so on its next
   dispatch.

If you are reporting a break, say which of those four it is, and attach the
`--dump-html` output: the exact response the parser was given, on success as
well as failure.

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once
   with a fresh `TRACTORSUPPLY_CDP_ENDPOINT` secret, AND once without it, to
   confirm the skip path runs too (CLAUDE.md §11). It also runs
   on a weekly schedule; it goes red with a 401 when the endpoint expires.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions. Its fixtures are real responses, cut down, in
`fixtures_generated.json`, which `make_fixtures.py` regenerates from a
capture directory and proves parse identically to the originals. Copy the
nearest existing check and edit it.

Six properties in this repo exist because they were measured against
expectation and cost real time. Tests pin all six, so a PR that breaks one
fails rather than silently regressing:

- **`price` is not the field called `price`.** On a multi-variant product the
  endpoint's `price` is one variant's — the MAXIMUM, on most of the 53 of 249
  products where it differed from `offerPriceMin`. The row carries the range
  as `price` / `price_max`.
- **Prices are per store.** The request names the stores near `--zip`; one
  product in 37 measured was priced differently in Georgia and Texas. The
  store is in every row and the sidecar, and `diff_runs.py` refuses to
  compare two store sets.
- **A department's visible id is a decoy.** `/tsc/category/farm-ranch` shows
  `selectedEntry.value = 1001811`, and the search endpoint answers that with
  zero results. The id it filters on (26654) is the parent every child
  names.
- **A missing category is HTTP 500**, with the site's Next.js error page,
  not a 404 — so it is classified `missing` by what it holds, and not
  retried as a server fault.
- **A nonsense search returns products anyway** (100 pairs of Wrangler
  jeans for "qzxqzxvvv"), with nothing in the response to say so. The run
  measures how many rows mention the query and warns.
- **Stock is null for multi-variant products**, because the inventory stream
  lists none of them (0 of 108). Null, not guessed.

Plus the family's own invariants, which are not negotiable:

- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero rows — including a listing that genuinely
  holds nothing, which is a correct answer — `5` the data never arrived, `6`
  partial. A pipeline branches on these.
- **An EMPTY page is never retried and never counted as blocked.**
- **Credentials never reach argv or a log, and an exception message is a
  log.** The masker is global rather than first-occurrence: a Playwright
  connection error repeats the endpoint five times.
- **Merge in page order, not arrival order**, so concurrency cannot change
  the output.

### If your change needs a live run

Most do not: the suite covers the parser, the writers, the classifier, the
shared fetch loop and the CLI contract against real, cut-down responses. If
yours genuinely needs tractorsupply.com, say in the PR what you ran (engine,
mode, listing, ZIP), through which client, and what you got, including the
sidecar's `total_results`.

**Run more than the primary engine.** "Mirror them exactly" is a design
rule, not a verification. The fetch loop is shared (`page_flow.run_pages`),
but each engine's driver plumbing is its own, and only running it proves it.

## Scope

This repo reads **public catalogue data** on tractorsupply.com: category and
search listings, with the prices, stock and ratings the site shows an
anonymous visitor, fetched the way the site's own front end fetches them.

Out of scope: anything behind a login, anything that adds to a cart, places
an order or submits any other form, and anything that defeats a protection
rather than passing it the way an ordinary browser does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
