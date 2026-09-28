# Troubleshooting

Find your exit code first (`echo $?` straight after the run), then the
sidecar's `stop_reason` in `<out>.meta.json` if one was written.

## Exit 2 — bad usage, or a listing the site does not have

* **"tractorsupply.com lists no store near ZIP …"** — the store lookup came
  back empty (measured for 00000 and for Anchorage's 99501), and the search
  endpoint refuses a request that names no store. Use a ZIP in the lower 48.
* **"/tsc/catalog/X answered with the site's own error page"** — no such
  category. The site answers an unknown slug with **HTTP 500** and its
  Next.js error page, not a 404. The slug is the last part of the category's
  address.
* **"--url already names the listing"** — pass a URL or `--category` /
  `--search`, not both. (A `TRACTORSUPPLY_URL` in `.env` is only a default:
  a typed `--category` wins over it.)
* **"… carries facet / sort / filter"** — filters are not read from a URL,
  and dropping them would scrape a wider listing than the one you pointed
  at. Pass the listing without them; `--sort` sets the ordering.
* **Selenium: "This --cdp-endpoint carries credentials"** — chromedriver
  cannot send them. Use `playwright_scraper.py` or `puppeteer_scraper.py`.

## Exit 3 — blocked

* **`blocked_akamai`** — Akamai's "Access Denied". It is the answer this
  site gave every client this repo tried except the 2Captcha Scraping
  Browser with a US exit (the README's access table). If you got it through
  the Scraping Browser, try a different `pid`, or a fresh endpoint; if you
  got it from local Chromium, that is the measured behaviour, not a bug.
* The log says **where** it was refused: "the homepage landing" means the
  very first navigation, before any request was sent.

A `<out>_page<N>_debug.html` beside the output holds what came back.

## Exit 4 — zero rows

The listing genuinely has nothing in it. Nothing is written, so an earlier
good file is left alone; `--allow-empty` writes the empty file.

## Exit 5 — the data never arrived

* **HTTP 401 on connect** — the Scraping Browser endpoint's credentials
  expired. They last about a day.
* **HTTP 500 / `profile_locked` on connect** — another run holds that `pid`.
  A profile allows one live connection; the engines retry three times, 3 s
  apart, because a profile stays locked for about two seconds after a clean
  disconnect.
* **"The endpoint refused this request (…)"** — the site rejected the
  parameters and named why ("Invalid Input storeNumber", "Max pageSize
  200.", or a Java NullPointerException, which is how it answers a wrong
  pricing zone — under HTTP 200). The same request would be rejected again,
  so it is not retried. If the query looks right, the API changed: open a
  "Site changed" issue with `--dump-html` output.
* **"Gave up on …"** — a timeout or a dead proxy. The log names which.

## Exit 6 — partial

Some pages came back and a later one did not. The output holds what was
gathered and the sidecar lists `pages_failed` by number.

## A search returned products that have nothing to do with the query

The site does that: it answers a query it cannot match with unrelated
products rather than with nothing ("qzxqzxvvv" came back as 100 pairs of
Wrangler jeans). No field in the response says so. The run warns when fewer
than 20% of page 1 mention the query, and the sidecar records the share as
`query_relevance`.

## Prices differ from what I see on the site

Prices are per store. A run prices at the stores near `--zip` (default
37027, Brentwood TN) and records the store in every row; your browser
prices at whichever store it picked for you. One product in 37 measured
differed between a Georgia and a Texas store set.

## `in_stock` is empty on some rows

The site's inventory stream does not list multi-variant products (sizes,
colours) at all — 0 of 108 measured — so their stock is null rather than
guessed. `variant_count` says which rows those are.

## A department page (`/tsc/category/pet`) returns the whole department

That is what it is: a department's listing is every product in it (Pet was
14,678). Use a category under it for a narrower run.

## A run looks fine but a column is wrong

Re-run with `--dump-html response.ndjson`: it writes the exact response the
parser was given, on success too, so a parsing bug can be told apart from a
change in what the site sends.

## Duplicates dropped, or a row missing between two runs

A listing ordered by popularity can move while a run reads its pages. A
product that crosses a page boundary is fetched twice (the dedupe drops it
and the log says so), or not at all. That is the site moving, not the
scraper.
