#!/usr/bin/env python3
"""
smoke_test.py — the offline suite for tractorsupply-scraper.

One file of plain functions. `tests/test_smoke.py` wraps it as a single
pytest test so `pytest` works as an entry point without a second copy of the
checks.

    python3 smoke_test.py            run everything
    python3 smoke_test.py -v         print every check as it passes

It must pass with NO engine library installed at all: every
`import playwright_scraper` / `selenium_scraper` / `puppeteer_scraper` is
guarded and the skip is RECORDED, because "skipped, engine absent" reads
identically to a real import error. CI installs each engine in its own venv
and checks that engine imports.

THE FIXTURES ARE IN `fixtures_generated.json`, NOT INLINE. They are real
responses captured 2026-09-28, cut down by `make_fixtures.py`, which proves
each one parses identically to its untrimmed original. Search responses keep
a handful of their own products, verbatim; pages keep only the __NEXT_DATA__
paths the parser reads plus one real asset tag; refusals are verbatim.
"""

import argparse
import ast
import copy
import csv
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import types
from dataclasses import asdict, fields

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAILURES = []
PASSED = 0
SKIPS = []
VERBOSE = False


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAILURES.append("%s%s" % (name, (" — " + detail) if detail else ""))
        print("  FAIL %s%s" % (name, (" — " + detail) if detail else ""))


def equal(name, got, want):
    check(name, got == want, "got %r, want %r" % (got, want))


def skip(group, reason):
    SKIPS.append("%s: %s" % (group, reason))
    print("  SKIP %s — %s" % (group, reason))


FIXTURES_PATH = os.path.join(HERE, "fixtures_generated.json")
FIXTURES = json.load(open(FIXTURES_PATH, encoding="utf-8"))


def fx(name) -> str:
    """A fixture as the text the site returned."""
    return FIXTURES[name]["text"]


def status_of(name):
    return FIXTURES[name].get("status")


ENGINES = ("playwright_scraper", "selenium_scraper", "puppeteer_scraper")
DRIVER_IMPORTS = {"playwright_scraper": "playwright",
                  "selenium_scraper": "selenium",
                  "puppeteer_scraper": "pyppeteer"}


def _import_engine(name):
    try:
        return __import__(name)
    except ImportError as e:
        skip(name, "engine library absent (%s)" % e)
        return None


def _query(name):
    """The Query a fixture was captured under, stores included."""
    import product_parser as P
    f = FIXTURES[name]
    q = P.Query(**f["query"])
    st = f.get("stores")
    if st:
        q.stores = P.StoreSet(zip_code=st["zip_code"], store_ids=tuple(st["store_ids"]),
                              zone=st["zone"], state=st["state"])
    return q


def _rows(name, page=1):
    import product_parser as P
    return P.parse_page(fx(name), _query(name), page)


# ---------------------------------------------------------------------------
# The fixtures themselves
# ---------------------------------------------------------------------------

def check_fixture_corpus_is_real_and_scrubbed():
    expected = {"cat_p1", "cat_p2", "cat_past_end", "cat_unknown_id", "price_ga",
                "price_tx", "kw_p1", "kw_junk", "rejected_zone", "rejected_sort",
                "rejected_page_size", "rejected_no_channel", "rejected_no_store",
                "circuit_breaker",
                "stores_75001", "stores_99501", "page_category",
                "page_department_farm_ranch", "page_department_pet", "page_missing",
                "akamai_raw", "akamai_dom_cdp", "landing_cdp"}
    missing = expected - set(FIXTURES)
    check("every fixture the suite uses is in fixtures_generated.json",
          not missing, "missing %s" % sorted(missing))
    blob = json.dumps(FIXTURES)
    check("the corpus is not empty (a scan of nothing passes for the wrong reason)",
          len(blob) > 50000, "%d bytes" % len(blob))
    check("no 32-hex string survived the cut", not re.search(r"\b[0-9a-f]{32}\b", blob))
    for shape in ("riskified", "adobe_mc", "MCMID", "_abck", "bm_sz", "ak_bmsc", "akacd"):
        check("no %s (session material a captured page echoes)" % shape,
              shape.lower() not in blob.lower())
    check("every fixture names the capture it was cut from",
          all(f.get("source") for f in FIXTURES.values()))


# ---------------------------------------------------------------------------
# The parser, asserted on VALUES rather than on coverage (§10)
# ---------------------------------------------------------------------------

def check_a_category_page_parses_to_the_captured_values():
    rows = _rows("cat_p1")
    equal("4 products kept from the capture", len(rows), 4)
    r = rows[0]
    equal("sku is the partNumber", r.sku, "135429599")
    equal("item_sku is the default item, the number ending the URL", r.item_sku, "1354295")
    check("...which really does end the URL", r.url.endswith("-" + r.item_sku))
    equal("url", r.url, "https://www.tractorsupply.com/tsc/product/"
          "fimco-40-gal-4-nozzle-3-point-hitch-sprayer-5302941-1354295")
    equal("title", r.title, "Fimco 40 gal. 4 Nozzle 3 pt. Hitch Sprayer")
    equal("brand", r.brand, "Fimco")
    equal("price, currency", (r.price, r.currency), (549.99, "USD"))
    equal("original_price and the discount computed from the two",
          (r.original_price, r.discount_pct), (699.99, 21.4))
    equal("rating, review_count", (r.rating, r.review_count), (4.01, 789))
    equal("badge", r.badge, "SALE")
    equal("model number", r.model_number, "5302941")
    equal("stock at store 2293: in stock, ships, no pickup", (r.in_stock, r.ship_available,
          r.pickup_available), (True, True, False))
    equal("category path from the category page, not guessed",
          r.category, "Farm & Ranch > Sprayers > 3 Point Sprayers")
    equal("the store the price is for", (r.store_id, r.zip_code), ("2293", "30013"))
    equal("price_source", r.price_source, "api")
    r = rows[1]
    equal("an In-Stores-Only product the inventory marks UNAVAILABLE",
          (r.sold_via, r.in_stock, r.ship_available), ("In Stores Only", False, False))
    r = rows[2]
    equal("an unreviewed product: rating and count both null, not 0",
          (r.rating, r.review_count), (None, None))
    equal("a NEWARRIVAL ribbon", r.badge, "NEWARRIVAL")
    equal("no struck price -> no original_price, no discount",
          (r.original_price, r.discount_pct), (None, None))
    rows2 = _rows("cat_p2", 2)
    equal("a product the site will not sell online is buyable=False",
          [r.buyable for r in rows2], [True, False, False])


def check_price_is_the_low_end_not_the_field_called_price():
    """Measured on 249 products: the endpoint's `price` equalled
    offerPriceMin on 196 and was the MAXIMUM on most of the rest."""
    import product_parser as P
    rows = _rows("kw_p1")
    raw = [e.get("price") for e in P._entries(P.catalog_record(fx("kw_p1")))]
    equal("the endpoint's own `price` field, as captured", raw, [25.99, 43.99, 26.49, 48.99])
    equal("price = the low end of the range", [r.price for r in rows],
          [25.99, 31.99, 26.49, 48.99])
    equal("price_max = the high end", [r.price_max for r in rows],
          [25.99, 43.99, 49.99, 48.99])
    check("the case this guards is in the fixture (not vacuous)",
          raw[1] != rows[1].price)
    r = rows[2]
    equal("a MAP-priced product names its handling", r.map_pricing, "CHECKOUT_ONLY")
    equal("...and a list price on a RANGE is not a was-price",
          (r.original_price, r.discount_pct), (None, None))
    r = rows[3]
    equal("a single-SKU product's list price IS one", (r.original_price, r.discount_pct),
          (53.99, 9.3))
    equal("unit price", (r.price_per_unit, r.price_unit), (1.63, "lb"))
    equal("search rows have no category", {x.category for x in rows}, {None})


def check_values_the_site_did_not_state_stay_null():
    rows = _rows("kw_p1")
    multi = [r for r in rows if (r.variant_count or 0) > 1]
    check("the fixture holds multi-variant products (not vacuous)", len(multi) >= 3)
    equal("the inventory stream lists no multi-variant product: stock stays null",
          {(r.in_stock, r.ship_available, r.pickup_available, r.item_sku) for r in multi},
          {(None, None, None, None)})
    import product_parser as P
    q = _query("cat_p1")
    e = copy.deepcopy(P._entries(P.catalog_record(fx("cat_p1")))[2])
    e["xf_prdRating"] = 0
    row = P.parse_entry(e, q, {}, page=1, position=1)
    equal("a 0 rating with no reviews is 'unreviewed', not a grade (§21)", row.rating, None)
    e = copy.deepcopy(P._entries(P.catalog_record(fx("cat_p1")))[0])
    e["offerPriceMin"] = e["offerPriceMax"] = None
    e.pop("displayPrice", None)
    e["price"] = None
    row = P.parse_entry(e, q, {}, page=1, position=1)
    equal("no price anywhere: price AND currency null, never a defaulted USD",
          (row.price, row.currency), (None, None))


def check_prices_are_per_store():
    ga, tx = _rows("price_ga"), _rows("price_tx")
    equal("the same product", (ga[0].sku, tx[0].sku), ("215226999", "215226999"))
    equal("priced 1799.99 at the Georgia stores and 1899.99 at the Texas ones",
          (ga[0].price, tx[0].price), (1799.99, 1899.99))
    equal("...and each row says which store", (ga[0].store_id, tx[0].store_id),
          ("2293", "2661"))


def check_totals_are_read_and_pages_planned():
    import product_parser as P
    import page_flow as F
    equal("resultsFound", P.total_results(fx("cat_p1")), 37)
    equal("37 at 24 a page is 2 pages", P.pages_available(37, 24), 2)
    equal("a search's total", P.total_results(fx("kw_p1")), 1076)
    equal("asking for more pages than exist plans the real number",
          F.pages_to_plan(10, 2), 2)
    equal("an empty listing still plans one page", F.pages_to_plan(3, 0), 1)
    equal("an unknown category id is an honest 0", P.total_results(fx("cat_unknown_id")), 0)
    equal("the page size default is the site's own", P.DEFAULT_PAGE_SIZE, 48)
    equal("...and the ceiling is the endpoint's", P.MAX_PAGE_SIZE, 200)


def check_position_counts_emitted_rows_not_payload_slots():
    import product_parser as P
    text = fx("cat_p1")
    recs = P.ndjson_records(text)
    for rec in recs:
        if rec.get("type") == "catalog":
            rec["data"]["catalogEntryView"].insert(1, {"partNumber": None, "name": None})
    bad = "\n".join(json.dumps(r) for r in recs)
    rows = P.parse_page(bad, _query("cat_p1"), 1)
    equal("a dropped record does not shift later positions",
          [r.position for r in rows], [1, 2, 3, 4])


def check_page_and_position_are_unique_across_pages():
    rows = _rows("cat_p1", 1) + _rows("cat_p2", 2)
    keys = [(r.page, r.position) for r in rows]
    equal("page+position unique across a two-page run", len(set(keys)), len(keys))


def check_relevance_warns_on_a_query_the_site_could_not_match():
    import product_parser as P
    equal("'dog food' rows mention the query", P.relevance_share(_rows("kw_p1"), "dog food"), 1.0)
    equal("'qzxqzxvvv' came back as Wrangler jeans: 0% relevant",
          P.relevance_share(_rows("kw_junk"), "qzxqzxvvv"), 0.0)
    check("...which is below the floor the run warns at",
          0.0 < __import__("page_flow").RELEVANCE_FLOOR)
    equal("no keyword words to test: no verdict", P.relevance_share(_rows("kw_p1"), "a"), None)


# ---------------------------------------------------------------------------
# Resolving the query: stores and category ids
# ---------------------------------------------------------------------------

def check_stores_are_read_from_the_zip_lookup():
    import product_parser as P
    s = P.parse_store_set(fx("stores_75001"), "75001")
    equal("six nearby stores, nearest first", s.store_ids,
          ("2661", "539", "444", "2379", "2209", "452"))
    equal("the zone and state come from the NEAREST store", (s.zone, s.state), ("13", "TX"))
    equal("...and its name is kept for the log", s.home_store_name, "LUCAS TX")
    equal("a ZIP with no store is None, not an empty set",
          P.parse_store_set(fx("stores_99501"), "99501"), None)
    equal("garbage is None", P.parse_store_set("<html>", "1"), None)


def check_category_ids_come_from_the_right_place():
    import product_parser as P
    equal("a category page: catIdDetails", P.category_details(fx("page_category")),
          {"id": "26628", "name": "3 Point Sprayers", "kind": "category",
           "path": "Farm & Ranch > Sprayers > 3 Point Sprayers", "slug": "3-point-sprayers"})
    d = P.category_details(fx("page_department_farm_ranch"))
    equal("a department: the parent every child names, prefixes stripped",
          (d["id"], d["kind"]), ("26654", "department"))
    nd = P.next_data(fx("page_department_farm_ranch"))
    decoy = nd["props"]["pageProps"]["pageProps"]["categoryDetails"]["selectedEntry"]["value"]
    equal("...NOT selectedEntry.value, which the endpoint answers with 0 results",
          decoy, "1001811")
    check("...and a child really names its parent with a catalogue prefix (not vacuous)",
          "10051_26654" in fx("page_department_farm_ranch"))
    equal("a second department", P.category_details(fx("page_department_pet"))["id"], "439")
    equal("the 500 error page names no category", P.category_details(fx("page_missing")), None)


def check_url_shapes():
    import product_parser as P
    ok = {
        "https://www.tractorsupply.com/tsc/catalog/poultry-feed-treats": ("category", "poultry-feed-treats"),
        "https://tractorsupply.com/tsc/catalog/3-point-sprayers/": ("category", "3-point-sprayers"),
        "https://www.tractorsupply.com/tsc/category/pet": ("category", "pet"),
        "https://www.tractorsupply.com/tsc/search/dog%20food": ("search", "dog food"),
        "https://www.tractorsupply.com/tsc/search?searchTerm=chicken+feed": ("search", "chicken feed"),
        "https://www.tractorsupply.com/tsc/catalog/dog-food?utm_source=x": ("category", "dog-food"),
    }
    for url, (mode, what) in ok.items():
        q, why = P.query_from_url(url)
        equal("%s -> %s" % (url, mode), (q and q.mode, q and (q.slug or q.keyword)),
              (mode, what))
    refused = {
        "https://www.tractorsupply.com/tsc/product/fimco-40-gal-1354295": "product-detail",
        "https://www.tractorsupply.com/tsc/brand/fimco": "brand",
        "https://www.tractorsupply.com/tsc/catalog/dog-food?facet=Brand%3AX": "facet",
        "https://www.tractorsupply.com/tsc/catalog/dog-food?sort=4": "sort",
        "https://www.petsense.com/tsc/catalog/dog-food": "not a tractorsupply.com",
        "https://www.tractorsupply.com/tsc/store-locator": "not a page this repo reads",
    }
    for url, reason in refused.items():
        q, why = P.query_from_url(url)
        check("%s is refused, with the reason (%s)" % (url, reason),
              q is None and why and reason in why, repr(why))


def _args(**kw):
    base = dict(url=None, category=None, search=None, mode=None, sort=None,
                page_size=None, zip=None, pages=1, url_from_env=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _build(**kw):
    import page_flow as F
    errors = []

    def error(msg):
        errors.append(msg)
        raise SystemExit(2)
    try:
        return F.build_query(_args(**kw), error), None
    except SystemExit:
        return None, errors[0]


def check_the_query_is_validated_up_front():
    q, why = _build(category="Poultry-Feed-Treats/")
    equal("--category is normalised to the slug", q and q.slug, "poultry-feed-treats")
    equal("...priced at the documented default ZIP", q and q.zip_code, "37027")
    for kw, reason in (({"url": "https://www.tractorsupply.com/tsc/catalog/x", "category": "y"},
                        "already names the listing"),
                       ({"category": "x", "search": "y"}, "two different listings"),
                       ({}, "name a listing"),
                       ({"category": "x", "mode": "search"}, "disagrees"),
                       ({"category": "x", "zip": "1234"}, "ZIP"),
                       ({"category": "x", "page_size": 500}, "HTTP 400"),
                       ({"category": "x", "pages": 0}, "at least 1"),
                       ({"category": "bad slug!"}, "not a category slug")):
        q, why = _build(**kw)
        check("refused: %r (%s)" % (kw, reason), q is None and why and reason in why, repr(why))
    q, why = _build(url="https://www.tractorsupply.com/tsc/catalog/x", category="y",
                    url_from_env=True)
    equal("a TYPED --category beats a TRACTORSUPPLY_URL default (§3)",
          (q and q.slug, why), ("y", None))


def check_the_request_is_the_front_ends_own():
    import product_parser as P
    q = _query("cat_p1")
    req = P.request_for(q, 2)
    equal("the front end's headers", {k: req.headers[k] for k in ("channel", "x-api-version", "zoneid")},
          {"channel": "web", "x-api-version": "v3", "zoneid": "23"})
    check("storeNumber keeps its pipes literal, as the site's worker sends it",
          "storeNumber=2293|568|1581|1964|1131|2737" in req.url, req.url)
    check("a category is searched by its id", "q=26628&categoryId=26628" in req.url)
    check("page and size are the endpoint's own names",
          "pageNumber=2" in req.url and "pageSize=48" in req.url)
    for sponsored in ("pgname", "expname", "maxreq", "slots", "uagent"):
        check("no sponsored-listing parameter %r (no paid placement shifts a position)"
              % sponsored, sponsored + "=" not in req.url)
    k = P.request_for(_query("kw_p1"), 1)
    check("a keyword is searched as a keyword, escaped",
          "searchType=keyword" in k.url and "q=dog+food" in k.url and "categoryId=&" in k.url, k.url)
    equal("every --sort maps to a value the endpoint accepted (0-7)",
          sorted(P.SORTS.values()), list(range(8)))


# ---------------------------------------------------------------------------
# Page states, on real captures
# ---------------------------------------------------------------------------

def check_page_states_on_real_captures():
    import page_flow as F
    import product_parser as P
    expect = {"cat_p1": "content", "kw_p1": "content", "cat_past_end": "empty",
              "cat_unknown_id": "empty", "rejected_zone": "rejected",
              "rejected_sort": "rejected", "rejected_page_size": "rejected",
              "rejected_no_channel": "rejected", "rejected_no_store": "rejected",
              "akamai_raw": "blocked", "akamai_dom_cdp": "blocked"}
    for name, state in expect.items():
        equal("%s -> %s" % (name, state), F.classify(fx(name), status_of(name)), state)
    equal("a tripped circuit breaker is UNAVAILABLE, not a refused parameter",
          F.classify(fx("circuit_breaker"), 200), "unavailable")
    equal("the wrong-zone failure arrives under HTTP 200 and is still rejected",
          F.classify(fx("rejected_zone"), 200), "rejected")
    check("...and names the site's own complaint",
          "NullPointer" in (P.api_error(fx("rejected_zone")) or "")
          or "doubleValue" in (P.api_error(fx("rejected_zone")) or ""))
    equal("Akamai's refusal is caught WITHOUT its status (Selenium has none)",
          (F.classify(fx("akamai_raw"), None), F.classify(fx("akamai_dom_cdp"), None)),
          ("blocked", "blocked"))
    check("the raw refusal is entity-escaped and still caught (§20: both encodings)",
          "errors&#46;edgesuite" in fx("akamai_raw") and "errors.edgesuite.net" in fx("akamai_dom_cdp"))
    equal("the landing and the pages are documents the site served",
          [P.detect_document_state(fx(n), 200) for n in
           ("landing_cdp", "page_category", "page_department_pet")],
          ["content"] * 3)
    equal("the missing-slug page is HTTP 500 and still 'missing', not retried as a fault",
          P.detect_document_state(fx("page_missing"), 500), "missing")
    equal("Chromium's own error page is not the site", P.detect_document_state(
        "<html><title>www.tractorsupply.com</title>ERR_PROXY_CONNECTION_FAILED</html>", None),
        "unknown")


def check_markers_do_not_match_a_page_the_scraping_browser_served():
    """§24: commit a fixture fetched over CDP and assert the marker set
    scores zero on it — WITHOUT any extension strip, because there is none."""
    import product_parser as P
    landing = fx("landing_cdp")
    check("the CDP landing carries the extension's injected hunters (not vacuous)",
          landing.count("chrome-extension://") >= 10)
    check("...including the words a loose marker would match",
          "captcha" in landing.lower())
    equal("and no refusal marker fires on it", P.detect_bot_challenge(landing), None)
    equal("the bare word 'akamai' is not a marker (it is on served pages)",
          P.detect_bot_challenge("<script src='https://x.akamaihd.net/y.js'></script>"), None)


def check_state_policy():
    import page_flow as F
    equal("every state has a policy", sorted(F.STATE_POLICY),
          ["blocked", "challenge", "content", "empty", "missing", "rejected",
           "throttled", "unavailable", "unknown"])
    check("unavailable: retried, NOT blocked", F.should_retry("unavailable")
          and not F.counts_as_blocked("unavailable") and not F.should_parse("unavailable"))
    check("content and empty are parsed, never retried or blocked",
          all(F.should_parse(s) and not F.should_retry(s) and not F.counts_as_blocked(s)
              for s in ("content", "empty")))
    check("rejected: not retried, NOT blocked (a typo is not a proxy problem)",
          not F.should_retry("rejected") and not F.counts_as_blocked("rejected")
          and not F.should_parse("rejected"))
    check("missing: not retried, not blocked", not F.should_retry("missing")
          and not F.counts_as_blocked("missing"))
    check("throttled: retried, NOT blocked (§24)",
          F.should_retry("throttled") and not F.counts_as_blocked("throttled"))
    check("blocked and challenge: retried and blocked",
          all(F.should_retry(s) and F.counts_as_blocked(s) for s in ("blocked", "challenge")))
    check("no state asks for a solve: this repo has no captcha path",
          all("solve" not in p for p in F.STATE_POLICY.values()))
    equal("refusals are reported by name",
          [F.refusal_name(s) for s in ("blocked", "challenge")], ["akamai", "akamai-challenge"])
    check("the advice names the client that was served, not a solver",
          "Scraping Browser" in F.refusal_advice("blocked")
          and "solve" not in F.refusal_advice("blocked").lower())
    check("a CDP 401 is explained as expired credentials, not a held pid",
          "expired" in F.cdp_connect_hint("WebSocket error: 401 Unauthorized")
          and "pid" not in F.cdp_connect_hint("401 Unauthorized"))
    check("...and a 500 as a held pid", "pid" in F.cdp_connect_hint("HTTP 500"))
    check("a 401 is not retried, a locked profile is",
          not F.cdp_should_retry("401") and F.cdp_should_retry("500 profile_locked"))


def check_policy_constants_have_a_consumer():
    """§17: a policy constant nothing reads is the same defect as dead code."""
    src = open(os.path.join(HERE, "page_flow.py"), encoding="utf-8").read()
    engines = "".join(open(os.path.join(HERE, m + ".py"), encoding="utf-8").read()
                      for m in ENGINES)
    for constant in ("RETRY_ON_BLOCKED", "BLOCK_RETRIES_WITHOUT_POOL", "THROTTLE_RETRIES",
                     "THROTTLE_WAIT_S", "FETCH_TIMEOUT_MS", "CORE_FIELD_FLOOR",
                     "RELEVANCE_FLOOR", "CDP_CONNECT_ATTEMPTS", "CDP_LOCKED_WAIT_S",
                     "CDP_CONNECT_TIMEOUT_S"):
        uses = len(re.findall(r"\b%s\b" % constant, src))
        check("page_flow.%s is READ, not only defined" % constant,
              uses >= 2 or constant in engines, "%d occurrence(s)" % uses)


# ---------------------------------------------------------------------------
# The shared fetch loop, driven end to end with a fake driver
# ---------------------------------------------------------------------------

class _FakeOps:
    """page_flow's named operations, answering from fixtures by request.

    `search` maps a page number to a list of (status, text) answers, taken
    in turn (the last one repeats). The store lookup and the category page
    have one answer each.
    """

    def __init__(self, search=None, stores=None, page=None, landing=None,
                 landing_status=200):
        import product_parser as P
        self.P = P
        self.search = search or {}
        self.stores = stores or (200, fx("stores_75001"))
        self.page = page or (200, fx("page_category"))
        self.landing = landing or fx("landing_cdp")
        self.landing_status = landing_status
        self.landed = False
        self.pool = None
        self.gotos = self.relaunches = 0
        self.fetches = []

    def goto(self, url):
        self.gotos += 1
        return self.landing_status, None

    def document_text(self):
        return self.landing

    def wait_ms(self, ms):
        pass

    def fetch(self, req):
        if req.path == self.P.STORE_PATH:
            self.fetches.append("stores")
            status, text = self.stores
        elif req.path.startswith("/tsc/"):
            self.fetches.append("page")
            status, text = self.page
        else:
            self.fetches.append(req.page)
            answers = self.search.get(req.page) or [(200, fx("cat_past_end"))]
            status, text = answers.pop(0) if len(answers) > 1 else answers[0]
        return status, text, None, None

    def relaunch(self):
        self.relaunches += 1
        self.landed = False

    def proxy_failure(self, text):
        return ""

    def close(self):
        pass


def _run(ops, query=None, pages=3, **extra):
    import page_flow
    import product_parser as P
    with tempfile.TemporaryDirectory() as tmp:
        args = types.SimpleNamespace(
            pages=pages, retries=2, retry_delay=0, delay=0,
            proxy_block_retries=2, out=os.path.join(tmp, "out"), format="json",
            allow_empty=False, dump_html=None, url=None, cdp_endpoint=None,
            concurrency=1)
        for k, v in extra.items():
            setattr(args, k, v)
        q = query or P.Query("category", slug="3-point-sprayers", page_size=24)
        rc = page_flow.run_pages(lambda: ops, lambda o: None,
                                 lambda pages: ([], [], False), args, None, q, 1)
        meta_path = args.out + ".meta.json"
        meta = json.load(open(meta_path)) if os.path.exists(meta_path) else None
        rows = (json.load(open(args.out + ".json"))
                if os.path.exists(args.out + ".json") else None)
    return rc, meta, rows, ops


def check_the_shared_loop_end_to_end():
    import product_parser as P
    ops = _FakeOps(search={1: [(200, fx("cat_p1"))], 2: [(200, fx("cat_p2"))]})
    rc, meta, rows, ops = _run(ops, pages=5)
    equal("a served two-page category: exit 0", rc, 0)
    equal("...complete, planned from the site's total (37 at 24 = 2 pages)",
          (meta and meta["status"], meta and meta["pages_available"]), ("complete", 2))
    equal("...the order of requests: stores, category page, pages 1 and 2",
          ops.fetches, ["stores", "page", 1, 2])
    equal("...rows from both pages, in page order", [r["page"] for r in rows],
          [1, 1, 1, 1, 2, 2, 2])
    equal("...the browser landed ONCE for the run", ops.gotos, 1)
    q = meta["query"]
    equal("...the sidecar records the category it resolved",
          (q["category_id"], q["category_path"]),
          ("26628", "Farm & Ranch > Sprayers > 3 Point Sprayers"))
    equal("...and the stores the prices are for",
          (q["zip_code"], q["store_ids"][0], q["zone"]), ("37027", "2661", "13"))
    equal("...which every row carries too", {r["store_id"] for r in rows}, {"2661"})

    rc, meta, rows, ops = _run(_FakeOps(search={1: [(200, fx("rejected_zone"))]}))
    equal("a REJECTED page 1 (under HTTP 200): exit 5, the data never arrived", rc, 5)
    equal("...fetched once, not retried", ops.fetches, ["stores", "page", 1])
    equal("...and no sidecar beside no output", meta, None)

    ops = _FakeOps(search={1: [(200, fx("cat_p1"))],
                           2: [(200, fx("circuit_breaker")), (200, fx("cat_p2"))]})
    rc, meta, rows, ops = _run(ops, pages=2)
    equal("a circuit breaker on page 2, then the page: exit 0, complete",
          (rc, meta and meta["status"]), (0, "complete"))
    equal("...page 2 asked for twice", ops.fetches, ["stores", "page", 1, 2, 2])
    ops = _FakeOps(search={1: [(200, fx("cat_p1"))], 2: [(200, fx("circuit_breaker"))]})
    rc, meta, rows, ops = _run(ops, pages=2)
    equal("...and one that persists is a partial run that says so", (rc, meta["stop_reason"]),
          (6, "page_load_timeout"))

    rc, meta, rows, ops = _run(_FakeOps(search={1: [(403, fx("akamai_raw"))]}))
    equal("Akamai on page 1: exit 3", rc, 3)
    equal("...re-fetched once from a fresh browser (no pool)", ops.relaunches, 1)

    rc, meta, rows, ops = _run(_FakeOps(landing=fx("akamai_dom_cdp"), landing_status=403))
    equal("the HOMEPAGE refused: exit 3", rc, 3)
    equal("...before a single request was sent", ops.fetches, [])

    rc, meta, rows, ops = _run(_FakeOps(stores=(403, fx("akamai_raw"))))
    equal("the store lookup refused: exit 3, not a crash", rc, 3)

    rc, meta, rows, ops = _run(_FakeOps(search={1: [(200, fx("cat_unknown_id"))]}))
    equal("an EMPTY listing: exit 4, and nothing written", (rc, rows), (4, None))

    rc, meta, rows, ops = _run(_FakeOps(search={1: [(429, ""), (200, fx("cat_p1"))]}),
                               pages=1, retries=1)
    equal("a throttle, then the page, with --retries 1: exit 0 — a throttle wait "
          "spends its OWN budget (§24)", rc, 0)
    equal("...at the SAME exit (no relaunch)", ops.relaunches, 0)

    rc, meta, rows, ops = _run(_FakeOps(stores=(200, fx("stores_99501"))))
    equal("a ZIP with no store: exit 2 before any search", rc, 2)
    equal("...no search page was fetched", [f for f in ops.fetches if isinstance(f, int)], [])

    rc, meta, rows, ops = _run(_FakeOps(stores=(200, "<html>something else</html>")))
    equal("a store lookup that never came back as JSON: exit 5, NOT 'no store near "
          "this ZIP' (a planted control found that it was parsed as an answer)", rc, 5)

    rc, meta, rows, ops = _run(_FakeOps(page=(500, fx("page_missing"))))
    equal("a category that does not exist (HTTP 500 error page): exit 2", rc, 2)
    equal("...asked for once: a missing page is not a fault to retry",
          ops.fetches, ["stores", "page"])

    ops = _FakeOps(page=(200, fx("page_department_farm_ranch")),
                   search={1: [(200, fx("cat_p1"))]})
    rc, meta, rows, ops = _run(ops, P.Query("category", slug="farm-ranch"), pages=1)
    equal("a DEPARTMENT resolves to the id its children name", meta["query"]["category_id"],
          "26654")

    ops = _FakeOps(search={1: [(200, fx("kw_junk"))]})
    rc, meta, rows, ops = _run(ops, P.Query("search", keyword="qzxqzxvvv"), pages=1)
    equal("a search the site could not match still runs (exit 0)...", rc, 0)
    equal("...with its relevance recorded, so a consumer can see it",
          meta["query_relevance"], 0.0)
    equal("...and no category page is fetched for a search", ops.fetches, ["stores", 1])


def check_a_listing_that_ends_early_is_complete():
    ops = _FakeOps(search={1: [(200, fx("cat_p1"))], 2: [(200, fx("cat_past_end"))]})
    rc, meta, rows, ops = _run(ops, pages=2)
    equal("an empty page 2 of a planned 2 ends the run: exit 0", rc, 0)
    equal("...as complete, stopped on the DATA",
          (meta["status"], meta["stop_reason"]), ("complete", "end_of_listing"))
    ops = _FakeOps(search={1: [(200, fx("cat_p1"))], 2: [(200, fx("cat_p1"))]})
    rc, meta, rows, ops = _run(ops, pages=2)
    equal("the same products on two pages (a listing moving) are kept once", len(rows), 4)


def check_every_engine_implements_the_operations_page_flow_uses():
    """The fetch loop is shared, so an engine missing ONE operation fails
    only when a live run reaches it. The set is DERIVED from page_flow's own
    source (every `ops.<name>`), not listed by hand."""
    tree = ast.parse(open(os.path.join(HERE, "page_flow.py"), encoding="utf-8").read())
    used = {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "ops"}
    check("page_flow drives the engines through named operations (not vacuous)",
          {"goto", "fetch", "document_text", "relaunch", "wait_ms"} <= used,
          repr(sorted(used)))
    check("...and the fake driver this suite uses implements every one",
          all(hasattr(_FakeOps(), name) for name in used),
          repr(sorted(n for n in used if not hasattr(_FakeOps(), n))))
    for module in ENGINES:
        tree = ast.parse(open(os.path.join(HERE, module + ".py"), encoding="utf-8").read())
        ops_cls = next((n for n in tree.body
                        if isinstance(n, ast.ClassDef) and n.name == "_Ops"), None)
        if ops_cls is None:
            check("%s defines _Ops" % module, False)
            continue
        methods = {n.name for n in ops_cls.body if isinstance(n, ast.FunctionDef)}
        attrs = {t.attr for n in ast.walk(ops_cls) if isinstance(n, ast.Assign)
                 for target in n.targets for t in ast.walk(target)
                 if isinstance(t, ast.Attribute)
                 and isinstance(t.value, ast.Name) and t.value.id == "self"}
        missing = sorted(used - methods - attrs)
        check("%s._Ops provides every operation page_flow uses" % module,
              not missing, "missing %s" % missing)


# ---------------------------------------------------------------------------
# The output contract
# ---------------------------------------------------------------------------

FAMILY_PREFIX = ["source", "scraped_at", "url", "sku", "title", "brand", "price",
                 "currency", "original_price", "discount_pct", "rating",
                 "review_count", "in_stock", "image_url", "category", "price_source"]


def check_row_schema():
    from output_writer import Product, ROW_CLASS_BY_MODE, UNIQUE_BY_SKU_MODES
    names = [f.name for f in fields(Product)]
    equal("the family prefix is byte-identical and in order (§9)",
          names[:len(FAMILY_PREFIX)], FAMILY_PREFIX)
    check("page, position, mode and sort follow it",
          names[len(FAMILY_PREFIX):len(FAMILY_PREFIX) + 4] == ["page", "position", "mode", "sort"])
    check("the store the price is for is a column", {"store_id", "zip_code"} <= set(names))
    equal("both modes are Product rows", {m: c.__name__ for m, c in ROW_CLASS_BY_MODE.items()},
          {"category": "Product", "search": "Product"})
    equal("both modes are one row per sku", sorted(UNIQUE_BY_SKU_MODES), ["category", "search"])


def check_every_column_is_populated_somewhere():
    """§9: a column that is null on every row of every run should not exist.
    Checked against the fixtures, which were chosen to cover the cases."""
    from output_writer import Product
    rows = []
    for name in ("cat_p1", "cat_p2", "kw_p1", "kw_junk"):
        rows += _rows(name)
    for f in fields(Product):
        check("column %s is filled on at least one fixture row" % f.name,
              any(getattr(r, f.name) not in (None, "") for r in rows))


def check_csv_and_json_writers():
    from output_writer import Product, write_csv, write_json
    rows = _rows("cat_p1")
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "out.csv")
        write_csv(rows, csv_path, row_cls=Product)
        reader = list(csv.reader(open(csv_path, encoding="utf-8")))
        equal("CSV header matches the dataclass, in order", reader[0],
              [f.name for f in fields(Product)])
        equal("CSV holds every row", len(reader) - 1, len(rows))
        empty_csv = os.path.join(tmp, "empty.csv")
        write_csv([], empty_csv, row_cls=Product)
        equal("an EMPTY csv still carries its header",
              len(list(csv.reader(open(empty_csv, encoding="utf-8")))), 1)
        json_path = os.path.join(tmp, "out.json")
        write_json(rows, json_path)
        loaded = json.load(open(json_path, encoding="utf-8"))
        check("ids stay strings in JSON", isinstance(loaded[0]["sku"], str))
        check("a price stays a number", isinstance(loaded[0]["price"], float))


def check_exit_codes():
    import output_writer as O
    equal("3 blocked / 4 empty / 5 never obtained / 6 partial",
          (O.EXIT_BLOCKED, O.EXIT_NO_PRODUCTS, O.EXIT_FETCH_FAILED, O.EXIT_PARTIAL),
          (3, 4, 5, 6))
    check("end_of_listing is a COMPLETE stop reason (§24)",
          "end_of_listing" in O.COMPLETE_STOP_REASONS)
    check("api_rejected is NOT complete", "api_rejected" not in O.COMPLETE_STOP_REASONS)


def check_a_run_that_finds_nothing_writes_nothing():
    from output_writer import save
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        with open(prefix + ".json", "w", encoding="utf-8") as f:
            f.write('[{"sku": "yesterday"}]')
        equal("an empty run exits 4", save([], prefix, "json", allow_empty=False), 4)
        equal("...and leaves the previous good file alone",
              open(prefix + ".json", encoding="utf-8").read(), '[{"sku": "yesterday"}]')
        equal("--allow-empty writes it, and still reports exit 4",
              save([], prefix, "json", allow_empty=True), 4)


def check_diff_runs_tracks_the_real_columns():
    import diff_runs as D
    for mode in ("category", "search"):
        check("%s: tracked columns are derived and non-empty" % mode,
              len(D.tracked_fields(mode)) >= 10, repr(D.tracked_fields(mode)))
    t = D.tracked_fields("category")
    check("price, stock and badges are tracked",
          {"price", "price_max", "original_price", "in_stock", "badge"} <= set(t))
    check("position, store and ZIP are NOT (the ordering; the query)",
          not ({"position", "store_id", "zip_code"} & set(t)))
    old = [asdict(r) for r in _rows("price_ga")]
    new = [asdict(r) for r in _rows("price_tx")]
    result = D.diff_products(old, new)
    equal("one changed, with the price named",
          [(c["sku"], list(c["changes"])) for c in result["changed"]],
          [("215226999", ["price", "price_max"])])
    with tempfile.TemporaryDirectory() as tmp:
        a, b = os.path.join(tmp, "a.json"), os.path.join(tmp, "b.json")
        json.dump(old, open(a, "w"))
        json.dump(new, open(b, "w"))
        json.dump({"status": "complete", "query": {"store_ids": ["2293"]}},
                  open(a[:-5] + ".meta.json", "w"))
        json.dump({"status": "complete", "query": {"store_ids": ["2661"]}},
                  open(b[:-5] + ".meta.json", "w"))
        args = types.SimpleNamespace(old=a, new=b)
        check("two runs priced at DIFFERENT STORES are refused", not D._check_comparable(args))


def check_sidecar_shape():
    from output_writer import run_meta
    meta = run_meta(status="complete", stop_reason="completed", pages_requested=3,
                    pages_completed=3, pages_failed=[], products=144, mode="search",
                    source="tractorsupply.com", start_url="https://www.tractorsupply.com/x",
                    final_url="https://www.tractorsupply.com/y",
                    extra={"total_results": 1076, "pages_available": 23,
                           "query": {"keyword": "dog food"}})
    for key in ("status", "stop_reason", "pages_requested", "pages_completed",
                "pages_failed", "mode", "source", "total_results", "query"):
        check("the sidecar records %r" % key, key in meta)
    check("pages_failed is a LIST", isinstance(meta["pages_failed"], list))


# ---------------------------------------------------------------------------
# The engines — the checks CLAUDE.md §17 says to steal
# ---------------------------------------------------------------------------

def check_engines_import_their_driver_at_module_level():
    for module, driver in DRIVER_IMPORTS.items():
        tree = ast.parse(open(os.path.join(HERE, module + ".py"), encoding="utf-8").read())
        top = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top.add(node.module.split(".")[0])
        check("%s imports %s at MODULE level" % (module, driver), driver in top,
              "top-level imports: %s" % sorted(top))


def check_shared_calls_bind_against_the_real_signature():
    """§17's check #1. Every call from an engine (and diff_runs and page_flow
    itself) into a shared module is bound against the callee's real
    signature. A name that does not exist FAILS (§22). A name bound in the
    calling file shadows a same-named module."""
    import output_writer
    import page_flow
    import product_parser
    import proxy_pool
    targets = {"page_flow": page_flow, "product_parser": product_parser,
               "output_writer": output_writer, "proxy_pool": proxy_pool}
    bound = 0
    for module in ENGINES + ("diff_runs", "page_flow", "make_fixtures"):
        tree = ast.parse(open(os.path.join(HERE, module + ".py"), encoding="utf-8").read())
        local_names = {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
        direct = {}
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in targets:
                for alias in node.names:
                    direct[alias.asname or alias.name] = (targets[node.module], alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in targets:
                        aliases[alias.asname or alias.name] = targets[alias.name]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func, owner, attr = node.func, None, None
            if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                    and func.value.id in aliases and func.value.id not in local_names):
                owner, attr = aliases[func.value.id], func.attr
            elif isinstance(func, ast.Name) and func.id in direct:
                owner, attr = direct[func.id]
            if owner is None:
                continue
            if not hasattr(owner, attr):
                check("%s.%s exists (called from %s:%d)" % (owner.__name__, attr,
                      module, node.lineno), False, "AttributeError on a live run")
                continue
            callee = getattr(owner, attr)
            if not callable(callee):
                continue
            try:
                sig = inspect.signature(callee)
            except (TypeError, ValueError):
                continue
            if any(kw.arg is None for kw in node.keywords) or any(
                    isinstance(a, ast.Starred) for a in node.args):
                continue
            try:
                sig.bind(*[None] * len(node.args), **{kw.arg: None for kw in node.keywords})
                bound += 1
            except TypeError as e:
                check("%s:%d %s.%s(...) binds against its real signature"
                      % (module, node.lineno, owner.__name__, attr), False,
                      "%s; signature is %s" % (e, sig))
    check("the binding walk checked something (%d calls)" % bound, bound > 50,
          "only %d calls were bound — is the walk finding them?" % bound)


def _argparse_flags(module_name):
    tree = ast.parse(open(os.path.join(HERE, module_name + ".py"), encoding="utf-8").read())
    parsers = {"p"}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr in ("add_argument_group",
                                             "add_mutually_exclusive_group")):
            parsers.update(t.id for t in node.targets if isinstance(t, ast.Name))
    flags = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in parsers):
            flags.update(a.value for a in node.args if isinstance(a, ast.Constant)
                         and isinstance(a.value, str) and a.value.startswith("--"))
    return flags


# The family's flag contract (CLAUDE.md §9, including the five it omitted
# for months), minus the three below, plus this repo's own query flags.
CONTRACT_FLAGS = {
    "--url", "--pages", "--category", "--format", "--out", "--delay",
    "--retries", "--retry-delay", "--concurrency", "--proxy", "--proxy-file",
    "--proxy-rotate", "--proxy-shuffle", "--proxy-block-retries",
    "--twocaptcha-key", "--cdp-endpoint", "--allow-empty", "--dump-html",
    "--headless", "--headful", "--fingerprint", "--fp-country", "--fp-tags",
    "--locale", "--mode",
}
SITE_FLAGS = {"--search", "--sort", "--zip", "--page-size"}
# REMOVED BY DECISION, not forgotten: the solver flags configure a captcha
# path this repo does not have, because no refusal tractorsupply.com served
# (19 of them, 2026-09-28) carried a widget. A flag that does nothing looks
# configurable and is not (§3). Reinstating them is a decision that needs a
# captcha to solve first, and this check makes it one.
REMOVED_BY_DECISION = {"--captcha-api", "--solve-captcha", "--min-score"}


def check_engine_flag_sets():
    """§17's check #2: against the contract AND against each other, both
    ways. The exception list IS the documentation."""
    sets = {m: _argparse_flags(m) for m in ENGINES}
    for module, flags in sets.items():
        missing = (CONTRACT_FLAGS | SITE_FLAGS) - flags
        check("%s defines every contract flag" % module, not missing,
              "missing %s" % sorted(missing))
        check("%s does not define the removed solver flags" % module,
              not (flags & REMOVED_BY_DECISION), repr(sorted(flags & REMOVED_BY_DECISION)))
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    check("the README says why the solver flags are absent",
          "--solve-captcha" in readme and "does not implement" in readme)
    DOCUMENTED_DIFFERENCES = {"puppeteer_scraper": {"--chromium-path"}}
    names = sorted(sets)
    for i in range(len(names) - 1):
        a, b = names[i], names[i + 1]
        only_a = sets[a] - sets[b] - DOCUMENTED_DIFFERENCES.get(a, set())
        only_b = sets[b] - sets[a] - DOCUMENTED_DIFFERENCES.get(b, set())
        check("%s and %s define the same flags" % (a, b), not only_a and not only_b,
              "only in %s: %s; only in %s: %s" % (a, sorted(only_a), b, sorted(only_b)))
    check("the documented difference still exists (closing it must be a decision)",
          "--chromium-path" in sets["puppeteer_scraper"])


def check_banned_and_removed_flags():
    """Scoped to the engines. `--country` is banned: it could disagree with
    the exit, and the site is US-only anyway."""
    for module in ENGINES:
        source = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        for flag in ("--antidetect", "--country", "--country-code"):
            check("%s does not define %s" % (module, flag), '"%s"' % flag not in source)


def check_undefined_names_in_every_module():
    """§10: compileall proves a file PARSES, not that its names RESOLVE.
    Kept COARSE (pooled bindings) so it under-reports rather than invents."""
    import builtins
    for filename in sorted(f for f in os.listdir(HERE) if f.endswith(".py")):
        tree = ast.parse(open(os.path.join(HERE, filename), encoding="utf-8").read())
        defined = set(dir(builtins)) | {"__file__", "__name__", "__doc__",
                                        "__package__", "__spec__"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    defined.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        unresolved = sorted(used - defined)
        check("%s: every name resolves" % filename, not unresolved, repr(unresolved))


def check_no_statement_is_unreachable():
    """A statement after a return/raise/break/continue in the SAME block."""
    for filename in sorted(f for f in os.listdir(HERE) if f.endswith(".py")):
        tree = ast.parse(open(os.path.join(HERE, filename), encoding="utf-8").read())
        dead = []
        for node in ast.walk(tree):
            for fld in ("body", "orelse", "finalbody"):
                block = getattr(node, fld, None)
                if not isinstance(block, list):
                    continue
                for i, stmt in enumerate(block[:-1]):
                    if isinstance(stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                        dead.append(block[i + 1].lineno)
                        break
        check("%s: no statement the control flow can never reach" % filename,
              not dead, "first at line %d" % min(dead) if dead else "")


def _import_graph(entrypoint):
    local = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}
    seen, todo = set(), [entrypoint]
    while todo:
        name = todo.pop()
        if name in seen or name not in local:
            continue
        seen.add(name)
        tree = ast.parse(open(os.path.join(HERE, name + ".py"), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                todo.extend(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                todo.append(node.module.split(".")[0])
    return seen


def check_dockerfile_copies_everything_the_entrypoint_imports():
    dockerfile = open(os.path.join(HERE, "Dockerfile"), encoding="utf-8").read()
    copy_lines, joining = [], False
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if joining or stripped.upper().startswith("COPY "):
            copy_lines.append(stripped)
            joining = stripped.endswith("\\")
    copied = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\.py", " ".join(copy_lines)))
    missing = sorted(_import_graph("playwright_scraper") - copied)
    check("the Dockerfile COPYs every module playwright_scraper.py imports",
          not missing, "missing %s" % missing)
    check("the image does not carry the test suite", "smoke_test" not in copied)
    check("...nor a module this repo no longer has",
          all(os.path.exists(os.path.join(HERE, m + ".py")) for m in copied),
          repr(sorted(m for m in copied if not os.path.exists(os.path.join(HERE, m + ".py")))))


def check_pyproject_lists_exactly_the_modules():
    text = open(os.path.join(HERE, "pyproject.toml"), encoding="utf-8").read()
    listed = set(re.findall(r'^\s*"([a-z_]+)",?\s*$',
                            text.split("py-modules = [")[1].split("]")[0], re.M))
    actual = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")} - {"smoke_test",
                                                                       "make_fixtures"}
    equal("pyproject's py-modules are the repo's modules", sorted(listed), sorted(actual))


def check_env_example_documents_exactly_what_the_loader_reads():
    import env_config
    text = open(os.path.join(HERE, ".env.example"), encoding="utf-8").read()
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", text, re.M))
    read = set(env_config.ENV_KEYS)
    equal("the example and the loader name the same variables",
          sorted(documented), sorted(read))
    check("the per-site variables carry the TRACTORSUPPLY_ prefix",
          {"TRACTORSUPPLY_CDP_ENDPOINT", "TRACTORSUPPLY_PROXY", "TRACTORSUPPLY_URL"} <= read)


def check_a_copied_env_example_reads_as_UNSET():
    import env_config
    text = open(os.path.join(HERE, ".env.example"), encoding="utf-8").read()
    values = dict(re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", text, re.M))
    credentials = {"TWOCAPTCHA_KEY", "TRACTORSUPPLY_CDP_ENDPOINT", "TRACTORSUPPLY_PROXY"}
    before = dict(os.environ)
    try:
        for name, raw in values.items():
            os.environ[name] = raw
            got = env_config.env_value(name)
            if name in credentials:
                check("a copied .env.example leaves %s unset" % name, got is None, repr(got))
            else:
                check("...while %s stays a usable default" % name, got == raw.strip(), repr(got))
        os.environ["TWOCAPTCHA_KEY"] = "not-a-real-key-but-a-real-value"
        equal("a real value is still read", env_config.env_value("TWOCAPTCHA_KEY"),
              "not-a-real-key-but-a-real-value")
    finally:
        os.environ.clear()
        os.environ.update(before)


def check_credential_scan_is_one_implementation_invoked_from_both():
    script = os.path.join(HERE, ".github", "ci_checks.py")
    if not os.path.isdir(os.path.join(HERE, ".github")):
        # Inside the Docker image, which copies no .github at all. Triggered
        # by the WHOLE directory being absent, never by one file in it (§22).
        skip("ci_checks", "no .github directory (the image)")
        return
    check("the credential scan exists as a script", os.path.exists(script))
    workflow = open(os.path.join(HERE, ".github", "workflows", "tests.yml"),
                    encoding="utf-8").read()
    check("CI INVOKES the script rather than reimplementing it", "ci_checks.py" in workflow)
    result = subprocess.run([sys.executable, script, "--secret-check", "--sample-check"],
                            cwd=HERE, capture_output=True, text=True)
    check("the credential scan and sample check pass on this tree",
          result.returncode == 0, (result.stdout + result.stderr)[-600:])


def check_no_workflow_imports_the_code_inline():
    """A workflow calls ci_checks.py or the CLIs; it does not carry its own
    copy of a check that imports the code. binance-scraper's first push went
    red on exactly that, an inline import of a DONOR repo's row class."""
    wf_dir = os.path.join(HERE, ".github", "workflows")
    if not os.path.isdir(wf_dir):
        skip("workflows", "no .github directory (the image)")
        return
    local = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}
    pattern = re.compile(r"^\s*(?:from|import)\s+(%s)\b" % "|".join(sorted(local)), re.M)
    for name in sorted(os.listdir(wf_dir)):
        hits = pattern.findall(open(os.path.join(wf_dir, name), encoding="utf-8").read())
        check("%s imports no local module inline" % name, not hits, repr(hits))


def check_there_is_no_hex_exemption():
    """§24: nothing this repo writes is 32-hex, so nothing is exempt, and a
    hex string in even the most innocent-looking context still fails."""
    if not os.path.isdir(os.path.join(HERE, ".github")):
        skip("ci_checks", "no .github directory (the image)")
        return
    sys.path.insert(0, os.path.join(HERE, ".github"))
    import ci_checks as C
    equal("SITE_PUBLIC_IDS is empty", C.SITE_PUBLIC_IDS, ())
    equal("GENERATED_DATA_FILES is empty", C.GENERATED_DATA_FILES, ())
    hexkey = "0123456789abcdef" * 2
    check("a 32-hex inside a tractorsupply.com URL is still caught",
          bool(C.HEX32.search(C._without_site_ids(
              "https://www.tractorsupply.com/tsc/product/x-" + hexkey))))


# Assembled from pieces, so this file can be scanned like every other rather
# than exempted (§22: the file most likely to acquire a stray phrase is the
# one a wholesale exemption never reads).
BANNED_WORDING = (
    "cloud" + " browser", "anti" + "detect browser", "2scraper " + "Anti" + "detect Browser",
    "gate." + "2prx.com", "ANTI" + "DETECT_LOCAL_API",
)


def check_banned_wording():
    """§12, enforced by this test rather than by review."""
    scanned = 0
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache",
                                                "live", "captures", ".claude")
                   and not os.path.exists(os.path.join(root, d, "pyvenv.cfg"))]
        for filename in files:
            if not filename.endswith((".py", ".md", ".yml", ".yaml", ".txt",
                                      ".toml", ".html", ".example", ".json")):
                continue
            path = os.path.join(root, filename)
            text = open(path, encoding="utf-8", errors="replace").read().lower()
            scanned += 1
            for phrase in BANNED_WORDING:
                if phrase.lower() in text:
                    check("%s contains no banned phrase #%d" % (
                        os.path.relpath(path, HERE), BANNED_WORDING.index(phrase)), False)
    check("the banned-wording scan read the repo (%d files)" % scanned, scanned > 20)


def check_no_donor_residue():
    """The repo was scaffolded from binance-scraper. A fact about THAT site in
    a shipped file is a sentence about the wrong site, so it fails here
    rather than in a reader's head. Naming the sibling as the provenance of
    a measurement ("measured by binance-scraper") is fine and is not
    matched: the artefacts of its site are."""
    residue = ("binance.com", "p2p", "copy-trading", "copytrading", "announcement",
               "amazontask", "/bapi/", "aws-waf-token")
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache",
                                                "live", "captures", ".claude")
                   and not os.path.exists(os.path.join(root, d, "pyvenv.cfg"))]
        for filename in files:
            if filename in ("smoke_test.py", "CHANGELOG.md") or not filename.endswith(
                    (".py", ".md", ".yml", ".yaml", ".txt", ".toml", ".example", ".json")):
                continue
            path = os.path.join(root, filename)
            low = open(path, encoding="utf-8", errors="replace").read().lower()
            hits = [w for w in residue if w in low]
            check("%s carries nothing of the donor site" % os.path.relpath(path, HERE),
                  not hits, repr(hits))


def check_concurrency_with_the_browser_stubbed():
    """§10: a live run cannot always reach this machinery. Driven through the
    SHARED worker loop with the page fetch replaced."""
    import page_flow
    import queue as queue_mod
    fetched, lock = [], threading.Lock()
    real = page_flow.fetch_listing_page

    def fake(ops, args, pool, query, page_num, mask=None):
        with lock:
            fetched.append(page_num)
        o = page_flow.PageOutcome(page_num=page_num, url="u")
        o.products = [] if page_num >= 6 else [object()]
        o.state = "empty" if page_num >= 6 else "content"
        return o

    work = queue_mod.Queue()
    for n in range(2, 51):
        work.put(n)
    results, rlock, exhausted = [], threading.Lock(), threading.Event()
    args = types.SimpleNamespace(delay=0)
    page_flow.fetch_listing_page = fake
    try:
        threads = [threading.Thread(target=page_flow.worker_loop,
                                    args=(_FakeOps(), args, None, work, results,
                                          rlock, exhausted, "w%d" % i))
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    finally:
        page_flow.fetch_listing_page = real
    check("every page fetched was fetched exactly once", len(fetched) == len(set(fetched)))
    check("dispatch STOPPED at the end of the listing", exhausted.is_set())
    check("...so 49 queued pages cost far fewer fetches", len(fetched) < 15,
          "fetched %d" % len(fetched))
    equal("attempted + unattempted covers the whole queue",
          len(set(fetched)) + work.qsize(), 49)


def check_a_dead_worker_neither_hangs_nor_loses_its_siblings():
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    import page_flow

    def exploding(ops, args, pool, query, page_num, mask=None):
        if page_num == 3:
            raise RuntimeError("worker died")
        o = page_flow.PageOutcome(page_num=page_num, url="u")
        o.products = [object()]
        o.state = "content"
        return o

    class FakeOps(_FakeOps):
        def __init__(self, *a, **k):
            super().__init__()

        def open(self):
            return self

    class FakePlaywright:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False

    real = (page_flow.fetch_listing_page, engine._Ops, engine.sync_playwright)
    page_flow.fetch_listing_page = exploding
    engine._Ops = FakeOps
    engine.sync_playwright = lambda: FakePlaywright()
    try:
        results, unattempted, exhausted = engine._fetch_pages_concurrently(
            types.SimpleNamespace(delay=0), None, None, list(range(2, 8)), 3)
    finally:
        page_flow.fetch_listing_page, engine._Ops, engine.sync_playwright = real
    check("the dead worker's siblings still delivered their pages",
          len(results) >= 3, "%d results" % len(results))
    check("page 3 is not reported as a success", 3 not in [o.page_num for o in results])


def check_worker_pools_start_on_different_exits():
    import page_flow
    from proxy_pool import ProxyPool
    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"], rotate="per-run")
    equal("three workers start on three different exits",
          len({page_flow.worker_pool(pool, i).current for i in range(3)}), 3)
    equal("a missing pool stays missing", page_flow.worker_pool(None, 0), None)


def check_fingerprint_kwargs_are_ones_the_driver_accepts():
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    from fingerprint_client import playwright_context_kwargs
    import playwright.sync_api as pw_api
    sample = {"id": "x", "country": "US", "userAgent": "Mozilla/5.0 Chrome/140.0.0.0",
              "screen": {"width": 1920, "height": 1080},
              "timezone": "America/New_York", "language": "en-US", "devicePixelRatio": 2}
    kwargs = playwright_context_kwargs(sample)
    signature = inspect.signature(pw_api.Browser.new_context)
    unknown = [k for k in kwargs if k not in signature.parameters]
    check("every fingerprint kwarg is one new_context accepts", not unknown, repr(unknown))


def check_engines_do_not_evaluate_a_string_in_the_browser():
    """§18: wait_for_function evaluates a string, which a CSP without
    unsafe-eval kills. page.evaluate with a real function is fine."""
    for module in ENGINES:
        tree = ast.parse(open(os.path.join(HERE, module + ".py"), encoding="utf-8").read())
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        for banned in ("wait_for_function", "waitForFunction", "waitFor"):
            check("%s never CALLS %s" % (module, banned), banned not in called)


def check_fetch_js_is_one_request_in_three_dialects():
    """The one piece of JavaScript each engine spells its own way. It must
    make the same request: the same headers, timeout and cookie rule."""
    for module in ENGINES:
        src = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        js = re.search(r'FETCH_JS = """(.*?)"""', src, re.S)
        check("%s defines FETCH_JS" % module, js is not None)
        if not js:
            continue
        body = js.group(1)
        for needle in ('credentials: "include"', "AbortController", "JSON.parse(",
                       '"content-type"'):
            check("%s's fetch() carries %s" % (module, needle), needle in body)
        check("%s passes the request's own headers into it" % module,
              "req.headers_json" in src)


def check_credentials_never_reach_a_log():
    for module in ENGINES:
        engine = _import_engine(module)
        if engine is None:
            continue
        masked = engine._mask_credentials(
            "tried ws://u:supersecret@h1:9222 and ws://u:supersecret@h2:9222 "
            "and again ws://u:supersecret@h1:9222")
        check("%s masks EVERY occurrence" % module, "supersecret" not in masked, masked)
        check("%s keeps host and port" % module, "h1:9222" in masked and "h2:9222" in masked)
    from proxy_pool import mask
    masked = mask("http://user:secret@exit.example.com:2334")
    check("proxy_pool.mask hides the password", "secret" not in masked)
    check("proxy_pool.mask keeps the exit", "exit.example.com:2334" in masked)


def check_pyppeteer_answers_proxy_auth_through_cdp():
    """page.authenticate() needs Network.setRequestInterception, which
    current Chromium does not have (a sibling repo, 2026-09-24)."""
    src = open(os.path.join(HERE, "puppeteer_scraper.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    check("pyppeteer never calls page.authenticate", "authenticate" not in called)
    check("...and answers Fetch.authRequired, for the PROXY only",
          "Fetch.authRequired" in src and 'source == "Proxy"' in src)


def check_capability_claims_match_the_code():
    """§19: the most expensive bug this family can ship is a SENTENCE."""
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    low = readme.lower()
    for phrase in ("cannot be solved", "can't be solved", "is not solvable",
                   "solver is inapplicable", "no solver can", "impossible to scrape"):
        check("README: no %r — write 'this repo does not implement X'" % phrase,
              phrase not in low)
    check("no captcha module ships", not os.path.exists(os.path.join(HERE, "captcha_solver.py")))
    mentions = [m.start() for m in re.finditer(r"captcha solving", low)]
    check("every README mention of captcha solving says this repo does not implement it",
          mentions and all("does not implement" in low[i:i + 120] for i in mentions),
          "%d mention(s)" % len(mentions))


def check_readme_numbers_are_not_stale():
    """§17's check #4: a number the README states about the code is the
    code's number."""
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    from output_writer import Product
    for number in re.findall(r"(\d+)\s+columns", readme):
        equal("the README's '%s columns' is the row's size" % number,
              int(number), len(fields(Product)))
    import product_parser as P
    check("the README states the default ZIP the code uses", P.DEFAULT_ZIP in readme)
    check("...and the page-size ceiling", str(P.MAX_PAGE_SIZE) in readme)


_TREE_BEFORE = None


def _tree_state():
    result = subprocess.run(["git", "status", "--porcelain"], cwd=HERE,
                            capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return sorted(line for line in result.stdout.splitlines() if not line.endswith(".pyc"))


def check_no_test_mutates_the_working_tree():
    if _TREE_BEFORE is None:
        skip("git status", "not a git repository")
        return
    changed = sorted(set(_tree_state()) - set(_TREE_BEFORE))
    check("the suite itself changed nothing in the working tree", not changed, repr(changed))


CHECKS = [v for k, v in sorted(globals().items()) if k.startswith("check_")]


def main():
    global VERBOSE, _TREE_BEFORE
    parser = argparse.ArgumentParser(description="tractorsupply-scraper offline suite")
    parser.add_argument("-v", "--verbose", action="store_true")
    VERBOSE = parser.parse_args().verbose
    _TREE_BEFORE = _tree_state()
    for fn in CHECKS:
        if VERBOSE:
            print("\n== %s" % fn.__name__)
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — a broken check is a failure
            import traceback
            FAILURES.append("%s raised %s: %s" % (fn.__name__, type(e).__name__, e))
            print("  ERROR %s raised %s: %s" % (fn.__name__, type(e).__name__, e))
            if VERBOSE:
                traceback.print_exc()
    print("\n%d checks passed, %d failed, %d group(s) skipped."
          % (PASSED, len(FAILURES), len(SKIPS)))
    for line in SKIPS:
        print("  skipped: %s" % line)
    if FAILURES:
        print("\nFailures:")
        for line in FAILURES:
            print("  - %s" % line)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
