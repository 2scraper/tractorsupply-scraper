#!/usr/bin/env python3
"""
tractorsupply-scraper — Selenium edition (secondary engine)
===========================================================

The same scrape as playwright_scraper.py, driven through Selenium. It must
agree with its twins on exit codes, run status, and whether a run crashes or
spends money. The fetch loop that decides all three lives in page_flow.py
and is shared, so this file is browser plumbing and nothing else.

    --mode category   a catalogue listing, /tsc/catalog/{slug}
    --mode search     a keyword search,    /tsc/search/{keyword}

**Read this before choosing this engine: on tractorsupply.com it has not
been served.** The one client the site let through in testing (2026-09-28)
was the Scraping Browser API, and Selenium cannot connect to it — see the
first limit below. A local Chrome was refused from a datacentre and from a
US residential exit alike. The engine is kept for parity, and it reports
that refusal correctly (exit 3, `blocked_akamai`), which is what its live
test measured.

Three limits of this engine, stated here rather than left to be discovered.
None is a bug in this code and none can be fixed from here:

  * **Selenium cannot use an authenticated remote CDP endpoint.**
    chromedriver's `debuggerAddress` takes a bare `host:port` and has nowhere
    to put a password, so a credentialled --cdp-endpoint (the Scraping
    Browser API) is refused with exit 2 rather than connected to and
    silently failing.
  * **Selenium cannot authenticate a proxy at all.** `--proxy-server=`
    accepts no credentials. They are stripped and a warning says so.
  * **Selenium reports no HTTP status for a navigation.** The landing is
    therefore judged on what the document holds: the site's own assets, or
    Akamai's refusal markers. A fetch() does report its status, so every
    request after the landing is judged exactly as in the twins.

Usage
-----
    python selenium_scraper.py --category poultry-feed-treats --pages 3

Requires: pip install -r requirements.txt -r requirements-selenium.txt
          Selenium 4 fetches a matching chromedriver itself; a local Chrome
          or Chromium must be installed.
"""

import argparse
import logging
import queue
import re
import sys
import threading
import time
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options

from product_parser import MAX_PAGE_SIZE, MODES, SORTS, DEFAULT_ZIP
from output_writer import EXIT_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, mask, ROTATE_MODES,
                        ProxyError, split_credentials)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("selenium_scraper")

PAGE_LOAD_TIMEOUT = 60
# Longer than the fetch() timeout, so the page's own AbortController reports
# a slow request (as a transport error the loop retries) before Selenium's
# script timeout cuts the call off with an exception.
SCRIPT_TIMEOUT = page_flow.FETCH_TIMEOUT_MS // 1000 + 15

# Chromium's own names for "the proxy is the problem, not the site" (§8).
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH", "ERR_PROXY_CERTIFICATE_INVALID",
    "ERR_NO_SUPPORTED_PROXIES", "ERR_SOCKS_CONNECTION_FAILED",
)

# Selenium's dialect of the fetch() in playwright_scraper.FETCH_JS: a
# function BODY run by execute_async_script, with the arguments in
# `arguments` and the result handed to the callback Selenium appends last.
# Same request, same headers, same return shape, same AbortController
# timeout (§8).
FETCH_JS = """
var done = arguments[arguments.length - 1];
var url = arguments[0], method = arguments[1], body = arguments[2];
var ctl = new AbortController();
var timer = setTimeout(function () { ctl.abort(); }, arguments[4]);
var init = {method: method, credentials: "include", signal: ctl.signal,
            headers: JSON.parse(arguments[3])};
if (body !== null) {
  init.headers["content-type"] = "application/json";
  init.body = body;
}
fetch(url, init).then(function (r) {
  return r.text().then(function (t) {
    clearTimeout(timer);
    done({status: r.status, text: t, waf: null});
  });
}).catch(function (e) {
  clearTimeout(timer);
  done({status: 0, text: "", waf: null, error: String(e)});
});
"""


# Every `scheme://user:pass@` in a string, however many times it occurs (§8).
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _chrome_ua(version: str) -> str:
    """A desktop-Chrome UA naming the installed Chrome's own version (§8)."""
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version} Safari/537.36")


def _proxy_failure(text) -> str:
    text = str(text)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _cdp_host_port(endpoint: str) -> str:
    """`host:port` for chromedriver's debuggerAddress, or exit 2 with a reason."""
    parts = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
    if parts.username or parts.password:
        logger.error(
            "This --cdp-endpoint carries credentials (%s), and Selenium cannot "
            "send them: chromedriver's debuggerAddress is a bare host:port. "
            "Use playwright_scraper.py or puppeteer_scraper.py for a "
            "credentialed endpoint such as the Scraping Browser API — both "
            "authenticate on the WebSocket upgrade.",
            _mask_credentials(endpoint))
        sys.exit(2)
    host = parts.hostname or endpoint
    port = f":{parts.port}" if parts.port else ""
    return f"{host}{port}"


class _Ops:
    """One Chrome driver, exposed as page_flow's named operations.

    Same contract as playwright_scraper._Ops, including the rule that a
    rotation means a genuinely FRESH browser (§8).
    """

    def __init__(self, args, pool):
        self.args, self.pool = args, pool
        self.remote = bool(args.cdp_endpoint)
        self.driver = None
        self.landed = False

    def open(self):
        self.landed = False
        options = Options()
        if self.remote:
            options.debugger_address = _cdp_host_port(self.args.cdp_endpoint)
            logger.info("Attaching to an existing browser at %s.",
                        options.debugger_address)
            # No UA, no proxy, no fingerprint on this path: the remote browser
            # brings its own (§8).
            self.driver = webdriver.Chrome(options=options)
            self._apply_timeouts()
            return self

        if self.args.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1600,1000")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument(f"--lang={self.args.locale}")
        if self.pool:
            scrubbed, credentials = split_credentials(self.pool.current)
            options.add_argument(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(self.pool.current))
            if credentials:
                logger.warning(
                    "This proxy has credentials and SELENIUM CANNOT SEND "
                    "THEM: --proxy-server accepts an address only. They have "
                    "been stripped, so the exit will most likely refuse the "
                    "requests. Use playwright_scraper.py or "
                    "puppeteer_scraper.py for an authenticated proxy.")

        self.driver = webdriver.Chrome(options=options)
        self._apply_timeouts()
        version = self.driver.capabilities.get("browserVersion", "")
        if version:
            try:
                self.driver.execute_cdp_cmd(
                    "Network.setUserAgentOverride",
                    {"userAgent": _chrome_ua(version)})
            except WebDriverException as e:
                logger.debug("Could not override the user agent: %s", e)
        if self.args.fingerprint:
            self._apply_fingerprint()
        return self

    def _apply_timeouts(self):
        # Explicit, because a driver that stops answering otherwise hangs
        # the run (§8).
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        self.driver.set_script_timeout(SCRIPT_TIMEOUT)

    def _apply_fingerprint(self):
        """The SAME init script the other two engines install."""
        from fingerprint_client import get_fingerprint, playwright_init_script
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = (fp.get("userAgent") or {}).get("value")
        try:
            if ua:
                self.driver.execute_cdp_cmd("Network.setUserAgentOverride",
                                            {"userAgent": ua})
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": playwright_init_script(fp)})
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"),
                        fp.get("country"))
        except WebDriverException as e:
            logger.warning("Could not apply the fingerprint over CDP (%s) — "
                           "continuing without it.", e)

    # ---- page_flow's operations -----------------------------------------

    def goto(self, url: str):
        try:
            self.driver.get(url)
        except WebDriverException as e:
            raise page_flow.TransportError(_mask_credentials(str(e))) from None
        # No status and no headers from a Selenium navigation: see the
        # module docstring. The classifier reads the document instead.
        return None, None

    def document_text(self) -> str:
        try:
            return self.driver.page_source or ""
        except WebDriverException:
            return ""

    def wait_ms(self, ms: int) -> None:
        time.sleep(ms / 1000.0)

    def fetch(self, req, timeout_ms: int = page_flow.FETCH_TIMEOUT_MS):
        try:
            got = self.driver.execute_async_script(
                FETCH_JS, req.url, req.method, req.body_json, req.headers_json,
                timeout_ms)
        except WebDriverException as e:
            return None, "", None, _mask_credentials(str(e))
        if not isinstance(got, dict):
            return None, "", None, "fetch() returned nothing"
        if got.get("error"):
            return None, "", None, str(got["error"])
        return got.get("status"), got.get("text") or "", got.get("waf"), None

    def proxy_failure(self, text: str) -> str:
        return _proxy_failure(text)

    def relaunch(self):
        if self.remote:
            self.landed = False
            return
        self.close()
        self.open()

    def close(self):
        try:
            if self.driver is not None:
                # quit(), not close(): close() leaves the driver process
                # running, which a per-page rotation would leak once a page.
                self.driver.quit()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during driver teardown: %s", e)


def _fetch_pages_concurrently(args, pool, query, page_nums, concurrency: int):
    """Fetch `page_nums` across `concurrency` workers, each with its own
    driver and exit; the page loop is page_flow.worker_loop."""
    work = queue.Queue()
    for n in page_nums:
        work.put(n)
    results, results_lock = [], threading.Lock()
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        try:
            ops = _Ops(args, page_flow.worker_pool(pool, index)).open()
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
    return page_flow.run_pages(
        lambda: _Ops(args, pool).open(),
        lambda ops: ops.close(),
        lambda pages: _fetch_pages_concurrently(args, pool, args.query,
                                                pages, concurrency),
        args, pool, args.query, concurrency, _mask_credentials)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="tractorsupply.com listing scraper — categories and "
                    "searches, priced per store (Selenium edition)")
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
                       "remote browser supplies its own fingerprint.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
    except WebDriverException as e:
        # A remote browser that will not accept the attachment is a REMOTE
        # failure (exit 5), not a crash in this code (exit 1).
        text = _mask_credentials(str(e))
        if args.cdp_endpoint and ("cannot connect" in text.lower()
                                  or "debugger" in text.lower()):
            logger.error("Could not attach to --cdp-endpoint: %s", text)
            sys.exit(EXIT_API_ERROR)
        raise
