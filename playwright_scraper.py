#!/usr/bin/env python3
"""
tractorsupply-scraper — Playwright edition (primary engine)
===========================================================

Scrapes tractorsupply.com listings, category or search, from the site's own
search endpoint: price and price range per store, list price, unit price,
rating, review count, stock and pickup at the chosen store, brand, model
number, badges and promotions.

    --mode category   a catalogue listing, /tsc/catalog/{slug}
    --mode search     a keyword search,    /tsc/search/{keyword}

Three engines ship in this repo and they must agree on exit codes, run
status, and whether a run crashes or spends money. The shared decisions live
in output_writer.finish_run() and page_flow.py so they cannot drift apart.

What is different about Tractor Supply
--------------------------------------
* **Akamai refuses almost every client, and it does not say why.** Measured
  2026-09-28: plain HTTP, local Chromium headless and headful, from a
  datacentre and from a US residential exit, and the Scraper API, were all
  answered with Akamai's "Access Denied" (or a reset connection). The one
  client served was the Scraping Browser with a US exit — and even that is
  refused when it NAVIGATES to a /tsc/ page. What it is allowed to do is
  load the homepage and then call the site's endpoints with fetch(), the way
  the site's own front end does. So that is what this engine does: land on
  the homepage, then issue every request as a same-origin fetch().
  See product_parser's docstring for the full table.
* **The listing is not in the page.** A category page's HTML holds the
  category's id and no products; the grid is painted from a search endpoint
  that answers in newline-delimited JSON. The engine fetches the category
  page once, for its id, and every page of products from the endpoint.
* **Prices are per store.** Every request names the stores near `--zip`
  (default 37027, Brentwood TN), and one product in 37 measured was priced
  differently in Texas than in Georgia. The store is in every row.
* **No captcha.** Not one refusal carried a widget, so there is nothing a
  solver could buy, and this repo has no captcha path.

Usage
-----
    python playwright_scraper.py --cdp-endpoint "$TRACTORSUPPLY_CDP_ENDPOINT" \\
        --category poultry-feed-treats --pages 3

    python playwright_scraper.py --search "dog food" --sort price-asc --zip 75001

    python playwright_scraper.py --url https://www.tractorsupply.com/tsc/catalog/3-point-sprayers

Requires: pip install -r requirements.txt -r requirements-playwright.txt
          then: playwright install chromium   (only if NOT using --cdp-endpoint)
"""

import argparse
import logging
import queue
import re
import sys
import threading
import time

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from product_parser import MAX_PAGE_SIZE, MODES, SORTS, DEFAULT_ZIP, Query
from output_writer import EXIT_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")


# Chromium's own names for a proxy that could not be used. Distinguished
# from a timeout because the two want opposite responses (CLAUDE.md §8).
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_PROXY_AUTH_REQUESTED",
    "ERR_PROXY_CERTIFICATE_INVALID",
    "ERR_NO_SUPPORTED_PROXIES",
    "ERR_SOCKS_CONNECTION_FAILED",
    "ERR_MANDATORY_PROXY_CONFIGURATION_FAILED",
)

# The fetch() every request goes through. It is handed to `page.evaluate` as
# a FUNCTION, which Playwright sends through `Runtime.callFunctionOn` rather
# than evaluating a string, so it works under any Content-Security-Policy
# (§18). The headers are the request's own (the search endpoint refuses a
# request without `channel`, and fails under HTTP 200 without the right
# `zoneid`). The AbortController is the timeout: a browser fetch() has none
# of its own, and §8 requires every remote call to be bounded.
FETCH_JS = """
async ([url, method, body, headersJson, timeoutMs]) => {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const init = {method, credentials: "include", signal: ctl.signal,
                  headers: JSON.parse(headersJson)};
    if (body !== null) {
      init.headers["content-type"] = "application/json";
      init.body = body;
    }
    const r = await fetch(url, init);
    return {status: r.status, text: await r.text(), waf: null};
  } catch (e) {
    return {status: 0, text: "", waf: null, error: String(e)};
  } finally {
    clearTimeout(timer);
  }
}
"""


def _chrome_ua(chromium_version: str) -> str:
    """A desktop-Chrome UA naming the browser's OWN real version (§8)."""
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36")


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one."""
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _launch_local(pw, args, pool):
    """Launch our own Chromium on `pool`'s current exit; return (browser, context, page).

    A proxy rotation tears the whole browser down and calls this again:
    cookies a bot manager issued against one exit, replayed from another,
    are a stronger signal than either address alone (§8).
    """
    launch_kwargs = {"headless": args.headless}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    browser = pw.chromium.launch(**launch_kwargs)
    ctx_kwargs = {"user_agent": _chrome_ua(browser.version), "locale": args.locale}
    init_script = None
    if args.fingerprint:
        from fingerprint_client import (get_fingerprint,
                                        playwright_context_kwargs,
                                        playwright_init_script)
        fp = get_fingerprint(args.twocaptcha_key,
                             tags=args.fp_tags, country=args.fp_country)
        ctx_kwargs.update(playwright_context_kwargs(fp))
        init_script = playwright_init_script(fp)
        logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))

    context = browser.new_context(**ctx_kwargs)
    if init_script:
        context.add_init_script(init_script)
    return browser, context, context.new_page()


class _Ops:
    """One browser + context + page, exposed as page_flow's named operations.

    page_flow owns the fetch loop for all three engines. This class answers
    only HOW Playwright does each step. `landed` records whether the page
    sits on a www.tractorsupply.com document that fetch() can be issued from;
    the loop owns it, and a relaunch clears it.
    """

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None
        self.landed = False

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool)
        self.landed = False
        return self

    # ---- page_flow's operations -----------------------------------------

    def goto(self, url: str):
        try:
            resp = self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except (PWTimeout, PWError) as e:
            raise page_flow.TransportError(_mask_credentials(str(e))) from None
        return (resp.status if resp is not None else None), None

    def document_text(self) -> str:
        try:
            return self.page.content()
        except PWError:
            return ""

    def wait_ms(self, ms: int) -> None:
        time.sleep(ms / 1000.0)

    def fetch(self, req, timeout_ms: int = page_flow.FETCH_TIMEOUT_MS):
        try:
            got = self.page.evaluate(
                FETCH_JS, [req.url, req.method, req.body_json, req.headers_json,
                           timeout_ms])
        except (PWError, PWTimeout) as e:
            return None, "", None, _mask_credentials(str(e))
        if not isinstance(got, dict):
            return None, "", None, "fetch() returned nothing"
        if got.get("error"):
            return None, "", None, str(got["error"])
        return got.get("status"), got.get("text") or "", got.get("waf"), None

    def proxy_failure(self, text: str) -> str:
        return _proxy_failure(text)

    def relaunch(self):
        """A fresh browser on the pool's current exit. On a remote browser
        only the landing is reset, since its exit is not ours to change."""
        if self.remote:
            self.landed = False
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()  # leave the remote browser app running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _connect_remote(pw, args):
    """Attach to an already-running browser over CDP; return (browser, context, page)."""
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
    browser, e = None, None
    for attempt in range(1, page_flow.CDP_CONNECT_ATTEMPTS + 1):
        try:
            browser = pw.chromium.connect_over_cdp(args.cdp_endpoint, timeout=30000)
            break
        except (PWError, PWTimeout) as err:
            e = err
            if attempt < page_flow.CDP_CONNECT_ATTEMPTS and page_flow.cdp_should_retry(str(err)):
                logger.warning("The Scraping Browser profile is still locked "
                               "(attempt %d/%d) — a previous run may be "
                               "releasing it; retrying in %.0fs.", attempt,
                               page_flow.CDP_CONNECT_ATTEMPTS,
                               page_flow.CDP_LOCKED_WAIT_S)
                time.sleep(page_flow.CDP_LOCKED_WAIT_S)
                continue
            break
    if browser is None:
        # The endpoint carries a password, and Playwright repeats it five
        # times in its error text (§8). Rewritten with it masked, keeping
        # host and port, which are the useful half.
        raise PWError(
            f"could not connect to --cdp-endpoint "
            f"{_mask_credentials(args.cdp_endpoint)}: "
            f"{_mask_credentials(str(e))}\n"
            f"{page_flow.cdp_connect_hint(str(e))}"
        ) from None
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    return browser, context, context.new_page()


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching GLOBALLY is the point: a Playwright connection error repeats the
# endpoint five times (§8).
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _fetch_pages_concurrently(args, pool, query: Query, page_nums, concurrency: int):
    """Fetch `page_nums` across `concurrency` workers.

    Each worker owns its own Playwright instance, browser and exit: with the
    sync API a browser belongs to the thread that made it, so sharing one is
    not an option even in principle (§7). The page loop itself is
    page_flow.worker_loop, shared by all three engines.
    """
    work = queue.Queue()
    for n in page_nums:
        work.put(n)
    results, results_lock = [], threading.Lock()
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        try:
            with sync_playwright() as pw:
                ops = _Ops(pw, args, page_flow.worker_pool(pool, index)).open()
                try:
                    page_flow.worker_loop(ops, args, query, work, results,
                                          results_lock, exhausted, name,
                                          _mask_credentials)
                finally:
                    ops.close()
        except Exception:  # noqa: BLE001 — a dead worker must not hang the run
            logger.exception("[%s] died; its pages will be reported as failed.", name)

    threads = [threading.Thread(target=worker, args=(i,), name=f"page-worker-{i + 1}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    unattempted = []
    while True:
        try:
            unattempted.append(work.get_nowait())
        except queue.Empty:
            break
    return results, sorted(unattempted), exhausted.is_set()


def scrape(args) -> int:
    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None
    concurrency = page_flow.concurrency_for(args, pool)
    with sync_playwright() as pw:
        return page_flow.run_pages(
            lambda: _Ops(pw, args, pool, remote=bool(args.cdp_endpoint)).open(),
            lambda ops: ops.close(),
            lambda pages: _fetch_pages_concurrently(args, pool, args.query,
                                                    pages, concurrency),
            args, pool, args.query, concurrency, _mask_credentials)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="tractorsupply.com listing scraper — categories and "
                    "searches, priced per store (Playwright edition)")
    p.add_argument("--mode", choices=list(MODES), default=None,
                   help="category or search. Inferred from --url / --category "
                        "/ --search; given explicitly, it must agree.")
    p.add_argument("--url", default=None,
                   help="A listing on the site: "
                        "https://www.tractorsupply.com/tsc/catalog/{slug} or "
                        "…/tsc/search/{keyword}. A URL carrying filters is "
                        "refused rather than scraped wider. Also read from "
                        "TRACTORSUPPLY_URL.")
    p.add_argument("--category", default=None, metavar="SLUG",
                   help="A category by its slug, the last part of its address "
                        "(poultry-feed-treats, 3-point-sprayers).")
    p.add_argument("--search", default=None, metavar="KEYWORD",
                   help="A keyword search. The site answers a query it cannot "
                        "match with unrelated products rather than nothing; "
                        "the run warns when page 1 does not mention the query.")
    p.add_argument("--sort", choices=list(SORTS), default=None,
                   help="Ordering (default popular, the site's own \"Most "
                        "Popular\"). Not cosmetic: a capped run holds the first "
                        "N products by this key.")
    p.add_argument("--zip", default=None,
                   help="US ZIP code whose nearby stores the prices and stock "
                        "are for (default %s, Brentwood TN). Prices differ "
                        "between stores." % DEFAULT_ZIP)
    p.add_argument("--page-size", type=int, default=None,
                   help="Products per request (default 48, as the site's own "
                        "grid; at most %d, which the endpoint enforces)."
                        % MAX_PAGE_SIZE)
    p.add_argument("--pages", type=int, default=1,
                   help="Pages to fetch. Planned against the total the site "
                        "states on page 1, so asking for more than exist "
                        "fetches all of them.")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Delay between pages, seconds (default %(default)s)")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Fetch pages through N parallel workers (default 1). "
                        "Each worker runs its own browser and holds its own "
                        "proxy exit. Ignored with --cdp-endpoint.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per request on a transport failure (default "
                        "3). The pause doubles each time. A request the "
                        "endpoint REFUSED is not retried.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry, doubling thereafter.")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="tractorsupply_products", help="Output file prefix")
    p.add_argument("--locale", default="en-US",
                   help="Browser locale (default en-US).")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 "
                        "(2captcha.com/proxy)")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run",
                   help="per-run (default): one exit for the whole run. "
                        "per-page: a new exit, and a fresh browser, per page.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a request is refused (Akamai), retry it from this "
                        "many OTHER exits (default 2). Needs a pool of more "
                        "than one.")
    p.add_argument("--twocaptcha-key", default=None,
                   help="2captcha.com API key, for --fingerprint. This repo "
                        "solves no captcha: the site showed none.")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Apply a browser fingerprint from 2captcha's "
                        "Fingerprint API. Needs --twocaptcha-key. Ignored with "
                        "--cdp-endpoint.")
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400. (default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP "
                        "instead of launching Chromium, e.g. the Scraping "
                        "Browser API endpoint ws://user:pass@host:port with "
                        "country-us. The one client the site served in "
                        "testing. --proxy and --headless/--headful are ignored.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact response the parser is given, on "
                        "success as well as failure. It is newline-delimited "
                        "JSON; the flag keeps the family's name.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args(argv)
    typed_url = args.url
    env_config.apply(args)
    args.url_from_env = typed_url is None and args.url is not None
    args.query = page_flow.build_query(args, p.error)
    args.mode = args.query.mode
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint API "
                     "uses the same key, though it's a separate subscription).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own fingerprint.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
    except PWError as e:
        # A remote browser refusing the connection is a REMOTE API failure
        # (exit 5), not a crash in this code (exit 1).
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
