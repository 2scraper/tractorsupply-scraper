"""
product_parser.py
-----------------
Everything this repo knows about tractorsupply.com lives here (CLAUDE.md §1).

Two modes, one of the site's own endpoints
------------------------------------------
    --mode category   a catalogue listing, /tsc/catalog/{slug}
    --mode search     a keyword search,    /tsc/search/{keyword}

Both are read from the endpoint the site's own listing page calls from a web
worker (/plp/workers/plp-search-worker.js):

    GET /gtwy/SiteSearch/catalogSearch?searchType=category|keyword
        &storeNumber=2293|568|...&pageNumber=N&pageSize=48&q=...&sort=0
        headers: zoneid, channel: web, x-api-version: v3

It answers with NEWLINE-DELIMITED JSON, not one document: a `thread_start`
record, a `catalog` record holding the products, an `oms_inventory` record
holding stock per SKU and fulfilment type, and a `thread_end` record.

Why that endpoint, and why through a browser
--------------------------------------------
Measured 2026-09-28. Akamai Bot Manager fronts the whole site, and:

    every URL, plain curl, datacentre (Hetzner FI)      403 "Access Denied"
    the same, real Chromium headless and headful        403
    US residential exit (2Captcha), curl                connection reset
    US residential exit, headless Chromium              ERR_HTTP2_PROTOCOL_ERROR
    US residential exit, headful Chromium               homepage 200, every
                                                        /tsc/ page and fetch 403
    2Captcha Scraper API (its own exits)                target HTTP 403
    Scraping Browser, country-us, NAVIGATING to /tsc/   403
    Scraping Browser, homepage, then same-origin fetch  200, every route tried

So the one thing that worked is: land on the homepage, then issue every
request as a same-origin `fetch()` from it. That is what the site's own
front end does, which is presumably why it is let through.

And the data could not come from the pages anyway. A /tsc/catalog page
fetched that way is 550 KB of HTML whose __NEXT_DATA__ holds the category's
id, name and breadcrumb and NO products: the grid, and the ItemList JSON-LD
that describes it, are painted client-side from the endpoint above. (An
archived copy of a category page does carry a full ItemList JSON-LD. That
copy is a script-free prerender served to a crawler, not what a browser
gets.) The category page IS fetched once per run, for its id.

Prices are per STORE
--------------------
The request names up to six nearby stores and their pricing zone, which the
site derives from a ZIP code (/wcs/resources/store/10151/zipcode/
fetchstoredetails). Measured on one 37-product category: a Georgia store set
and a Texas one agreed on 36 prices and differed on 1 (1799.99 vs 1899.99).
So a run's store is part of its query: `--zip` picks it, and it is recorded
in the sidecar and in every row's `store_id`.

What the endpoint does with a wrong value
-----------------------------------------
    no `channel` header          HTTP 400 "channel is mandatory field"
    no `storeNumber`             HTTP 400 "Invalid Input storeNumber"
    storeNumber, zoneid=1        HTTP 200, and the `catalog` record carries a
                                 Java NullPointerException in `error` and no
                                 products — a failure under a success status
    sort=9                       HTTP 400 "Invalid Input sort parameter"
    pageSize=500                 HTTP 400 "Max pageSize 200."
    an unknown category id       HTTP 200, resultsFound 0: an honest empty
    pageNumber past the end      HTTP 200, no products: an honest empty
    a nonsense keyword           HTTP 200, 100 results of something else
                                 (Wrangler jeans for "qzxqzxvvv"), and nothing
                                 in the metadata says so

The first five are `rejected`. The last is not detectable from the response,
so `relevance_share()` measures it from the rows instead and the engine warns.
"""

import html as html_lib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from output_writer import Product, SOURCE_DEFAULT

BASE = "https://www.tractorsupply.com"

# Where a browser engine lands before its first request: the homepage. It is
# the one document on the site that answered a Scraping Browser navigation
# with 200 (4 of 4, 2026-09-28) while every /tsc/ route answered 403. Every
# request after it is a same-origin fetch() from this document.
ORIGIN_URL = BASE + "/"

SEARCH_PATH = "/gtwy/SiteSearch/catalogSearch"
STORE_PATH = "/wcs/resources/store/10151/zipcode/fetchstoredetails"

# ---------------------------------------------------------------------------
# Paging and ordering, measured
# ---------------------------------------------------------------------------

# The site's own grid asks for 48. The endpoint accepts up to 200 and refuses
# anything larger with HTTP 400 ("Pagination limit exceeded: Max pageSize
# 200."); a larger page is fewer requests for the same rows.
DEFAULT_PAGE_SIZE = 48
MAX_PAGE_SIZE = 200

# A ceiling on --pages, so a typo cannot start a ten-thousand-request run.
# The deepest listing measured was a 1,076-result search, 23 pages of 48,
# and every page of it answered.
MAX_PAGES = 500

# --sort -> the endpoint's `sort`. The labels and values are the site's own,
# from the listing page's sort menu (AEM `product-grid-cf.sort.sortfilter`).
# Each of 0-7 answered 200 on 2026-09-28, and 1-5 and 7 each put a different
# product first; 9 is refused with HTTP 400.
SORTS = {
    "popular": 0,        # "Most Popular" — the site's default
    "rating": 1,         # "Ratings"
    "name-asc": 2,       # "Name: A to Z"
    "name-desc": 3,      # "Name: Z to A"
    "price-asc": 4,      # "Price: Low to High"
    "price-desc": 5,     # "Price: High to Low"
    "recency": 6,        # "Recency"
    "new": 7,            # "New Arrivals"
}
DEFAULT_SORT = "popular"

# The ZIP a run prices against when --zip is not given. A run has to name
# SOME store, and leaving it to the site would mean leaving it to the exit's
# geolocation, so two runs from two exits would price the same catalogue
# against two different stores and diff as price changes. 37027 is
# Brentwood, Tennessee, where Tractor Supply is headquartered: a fixed,
# documented choice rather than a guess presented as a fact, and recorded in
# every row.
DEFAULT_ZIP = "37027"
# How many nearby stores to name in a request. The site's own worker names
# six (the header store and five nearby); the API needs at least one.
STORES_PER_REQUEST = 6

MODES = ("category", "search")

# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


@dataclass
class ApiRequest:
    """One GET to the site: what an engine's fetch() sends.

    `headers` are the front end's own. The search endpoint refuses a request
    without `channel`, and returns a failure under HTTP 200 without the
    right `zoneid` (module docstring), so they are part of the request, not
    decoration.
    """
    path: str
    page: int
    params: Dict[str, Any] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    method: str = "GET"
    body: Optional[Dict[str, Any]] = None

    @property
    def url(self) -> str:
        # The site's own worker leaves `|` in storeNumber unescaped, so this
        # does too: `safe` keeps it literal.
        q = ("?" + urlencode(self.params, safe="|")) if self.params else ""
        return BASE + self.path + q

    @property
    def body_json(self) -> Optional[str]:
        return None if self.body is None else json.dumps(self.body)

    @property
    def headers_json(self) -> str:
        return json.dumps(self.headers)

    @property
    def label(self) -> str:
        return "%s page=%d" % (self.path.rsplit("/", 1)[-1], self.page)


@dataclass
class StoreSet:
    """The stores a run prices against, as the ZIP lookup returned them."""
    zip_code: str
    store_ids: Tuple[str, ...]
    zone: str
    state: Optional[str] = None
    home_store_name: Optional[str] = None

    @property
    def home(self) -> str:
        return self.store_ids[0]


@dataclass
class Query:
    """What a run asks for, independent of the page number.

    Built from --url or from the flags and validated before anything is
    sent. `category_id` and `stores` are RESOLVED at run time, from the
    category page and the ZIP lookup, because neither is in the URL a user
    types.
    """
    mode: str
    slug: Optional[str] = None          # category
    keyword: Optional[str] = None       # search
    sort: str = DEFAULT_SORT
    page_size: int = DEFAULT_PAGE_SIZE
    zip_code: str = DEFAULT_ZIP
    category_id: Optional[str] = None
    category_name: Optional[str] = None
    category_path: Optional[str] = None
    stores: Optional[StoreSet] = None

    def validate(self) -> Optional[str]:
        """None when the query can be sent, else the reason it cannot."""
        if self.mode == "category":
            if not self.slug or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", self.slug):
                return ("--category %r is not a category slug: the last part of a "
                        "/tsc/catalog/{slug} address, e.g. poultry-feed-treats"
                        % self.slug)
        elif self.mode == "search":
            if not (self.keyword or "").strip():
                return "--search needs a keyword"
        else:
            return "unknown mode %r" % self.mode
        if self.sort not in SORTS:
            return "--sort must be one of %s" % ", ".join(SORTS)
        if not (1 <= int(self.page_size) <= MAX_PAGE_SIZE):
            return ("--page-size must be 1-%d: the endpoint refuses anything "
                    "larger with HTTP 400" % MAX_PAGE_SIZE)
        if not re.fullmatch(r"\d{5}", self.zip_code or ""):
            return "--zip %r is not a 5-digit US ZIP code" % self.zip_code
        return None

    @property
    def page_url(self) -> str:
        """The site page this query is the listing of."""
        if self.mode == "category":
            return "%s/tsc/catalog/%s" % (BASE, self.slug)
        return "%s/tsc/search/%s" % (BASE, quote(self.keyword or "", safe=""))


def store_request(zip_code: str) -> ApiRequest:
    return ApiRequest(STORE_PATH, 0, params={"responseFormat": "json",
                                             "zipCode": zip_code})


def category_page_request(query: Query) -> ApiRequest:
    """The category's own page, fetched once for its id (module docstring)."""
    return ApiRequest("/tsc/catalog/%s" % query.slug, 0,
                      headers={"accept": "text/html,application/xhtml+xml"})


def request_for(query: Query, page: int) -> ApiRequest:
    """The call that fetches page `page` (1-based) of `query`.

    Mirrors the site's own worker, minus the sponsored-listing parameters
    (`pgname`, `expname`, `maxreq`, …): without them `sponsoredListings`
    comes back null and every row is an organic result, so no paid placement
    can shift a position (§24).
    """
    stores = query.stores
    store_param = "|".join(stores.store_ids) if stores else ""
    if query.mode == "category":
        search_type, q, cat = "category", query.category_id or "", query.category_id or ""
    else:
        search_type, q, cat = "keyword", (query.keyword or "").strip(), ""
    params = {
        "searchType": search_type,
        "minAttr": "true",
        "storeNumber": store_param,
        "pageNumber": page,
        "pageSize": int(query.page_size),
        "q": q,
        "categoryId": cat,
        "sort": SORTS[query.sort],
    }
    if stores and stores.state:
        # Scopes the `oms_inventory` stream to the delivery state, as the
        # site's own request does.
        params["deliveryStoreIds"] = store_param
        params["state"] = stores.state
    headers = {"accept": "*/*", "channel": "web", "x-api-version": "v3",
               "index": "1", "zoneid": stores.zone if stores else "1"}
    return ApiRequest(SEARCH_PATH, page, params=params, headers=headers)


# ---------------------------------------------------------------------------
# --url
# ---------------------------------------------------------------------------

SUPPORTED_HOSTS = ("www.tractorsupply.com", "tractorsupply.com")

# Query parameters a listing URL may carry that change nothing this repo
# sends. Anything else (a facet, a filter) is refused, because silently
# dropping it would scrape a wider listing than the one the user pointed at.
_HARMLESS_PARAMS = {"page", "pagenumber", "cm_sp", "cm_mmc", "utm_source",
                    "utm_medium", "utm_campaign", "utm_content", "utm_term",
                    "gclid", "isintsrch", "searchterm"}


def query_from_url(url: str) -> Tuple[Optional[Query], Optional[str]]:
    """Map a tractorsupply.com address onto a Query: (query, None), or
    (None, reason) when the address is not one of the shapes this repo reads.

        www.tractorsupply.com/tsc/catalog/{slug}      category
        www.tractorsupply.com/tsc/search/{keyword}    search
        www.tractorsupply.com/tsc/search?searchTerm=  search (older form)
    """
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if host not in SUPPORTED_HOSTS:
        return None, ("%r is not a tractorsupply.com address."
                      % (parsed.hostname or url))
    path = parsed.path or "/"
    qs = {k.lower(): v[-1] for k, v in parse_qs(parsed.query).items()}
    extra = sorted(k for k in qs if k not in _HARMLESS_PARAMS)
    if extra:
        return None, ("%s carries %s. Filters, facets and sort parameters are "
                      "not read from a URL by this repo, and dropping them "
                      "would scrape a different listing than the one the URL "
                      "shows. Pass the listing without them (use --sort for "
                      "the ordering)." % (url, ", ".join(extra)))

    # /tsc/category/ is a department page; /tsc/catalog/{department}
    # redirects to it. Both are read the same way (category_details).
    m = re.fullmatch(r"/tsc/(?:catalog|category)/([a-z0-9][a-z0-9-]*)/?", path)
    if m:
        return Query(mode="category", slug=m.group(1)), None
    m = re.fullmatch(r"/tsc/search/([^/]+)/?", path)
    if m:
        return Query(mode="search", keyword=unquote(m.group(1)).strip()), None
    if re.fullmatch(r"/tsc/search/?", path) and qs.get("searchterm"):
        return Query(mode="search", keyword=qs["searchterm"].strip()), None
    if path.startswith("/tsc/product/"):
        return None, ("%s is a product page. This repo reads listings "
                      "(/tsc/catalog/…, /tsc/search/…); it does not implement "
                      "a product-detail mode." % url)
    if path.startswith("/tsc/brand/"):
        return None, ("%s is a brand page. This repo does not implement brand "
                      "listings; a search for the brand name "
                      "(/tsc/search/{brand}) reads the same catalogue." % url)
    return None, ("%s is not a page this repo reads. Supported: a category "
                  "(/tsc/catalog/{slug}) or a search (/tsc/search/{keyword})."
                  % url)


# ---------------------------------------------------------------------------
# Page state
# ---------------------------------------------------------------------------

# Akamai's refusal page, counted on 2026-09-28 over every refusal captured
# (curl, Chromium, the Scraping Browser, the Scraper API) and against every
# served document and endpoint response:
#     errors.edgesuite.net                                  on each refusal
#     "Reference #18.<hex>.<epoch>.<hex>"                   on each refusal
#     either one, on a served document or response          0
# The raw bytes spell the punctuation as entities
# (`errors&#46;edgesuite&#46;net`, `Reference&#32;&#35;18…`) while a browser
# DOM serialises it back out plain, so the text is unescaped over a bounded
# prefix before matching (§20).
_AKAMAI_REF_RE = re.compile(r"Reference\s*#\s*\d+\.[0-9a-f]+\.\d+\.[0-9a-f]+", re.I)
AKAMAI_DENY_MARKERS = ("errors.edgesuite.net",)

# Akamai Bot Manager's interactive challenge ("sec-cpt"). NOT OBSERVED on
# this site by this repo: every refusal met was the plain Access Denied page
# above. It is here because it is the other shape that vendor serves, and a
# challenge that a fresh browser may pass wants a retry rather than exit 3 on
# first sight. There is nothing in it for a captcha solver to buy.
AKAMAI_CHALLENGE_MARKERS = ("_sec/cp_challenge", "sec-if-cpt-container",
                            "sec-cpt-if")


def _head(text: Optional[str], n: int = 20_000) -> str:
    return html_lib.unescape((text or "")[:n])


def detect_bot_challenge(text: Optional[str], url: str = "") -> Optional[str]:
    """The vendor whose refusal or challenge this is, or None."""
    head = _head(text)
    if any(m in head for m in AKAMAI_CHALLENGE_MARKERS):
        return "akamai-challenge"
    if any(m in head for m in AKAMAI_DENY_MARKERS) or _AKAMAI_REF_RE.search(head):
        return "akamai"
    return None


def _json_or_none(text: Any) -> Any:
    if isinstance(text, (dict, list)):
        return text
    if not isinstance(text, str) or not text:
        return None
    s = text.strip()
    if not s or s[0] not in "{[":
        return None
    try:
        return json.loads(s)
    except ValueError:
        return None


def ndjson_records(text: Any) -> List[Dict[str, Any]]:
    """Every JSON object in a newline-delimited response, in order.

    Also accepts one plain JSON document (the endpoint's 400 answers are one
    object) and an already-decoded list or dict, so a fixture can be either.
    """
    if isinstance(text, dict):
        return [text]
    if isinstance(text, list):
        return [r for r in text if isinstance(r, dict)]
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    out: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line[0] != "{":
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def catalog_record(text: Any) -> Optional[Dict[str, Any]]:
    """The `catalog` record of a search response, or None."""
    for rec in ndjson_records(text):
        if rec.get("type") == "catalog":
            return rec
    return None


def inventory_record(text: Any) -> Optional[Dict[str, Any]]:
    for rec in ndjson_records(text):
        if rec.get("type") == "oms_inventory":
            return rec
    return None


def _entries(cat_rec: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    data = (cat_rec or {}).get("data")
    if not isinstance(data, dict):
        return []
    return [e for e in (data.get("catalogEntryView") or []) if isinstance(e, dict)]


def detect_page_state(text: Optional[str], status: Optional[int] = None,
                      url: str = "", waf_action: Optional[str] = None) -> str:
    """Name what the search endpoint answered with. See page_flow.STATE_POLICY.

        content     a `catalog` record with products in it
        empty       a `catalog` record with none, and no error: an answer
        rejected    the endpoint refused the PARAMETERS: `errorCode`, HTTP
                    400, or a `catalog` record carrying `error` (the wrong-
                    zone NullPointerException arrives under HTTP 200)
        challenge   Akamai's interactive challenge. Not observed (above).
        blocked     Akamai's Access Denied, or any 403
        throttled   429. Not observed on this site; the status's meaning.
        unknown     anything else

    Signals are ordered by what they PROVE (§17): the endpoint's own
    records first, because no refusal page carries them. `waf_action` is
    unused here and kept so every engine shares one call shape.
    """
    cat = catalog_record(text)
    if cat is not None:
        if cat.get("error"):
            return "rejected"
        return "content" if _entries(cat) else "empty"
    payload = _json_or_none(text)
    if isinstance(payload, dict) and ("errorCode" in payload or "errorMessage" in payload):
        return "rejected"
    vendor = detect_bot_challenge(text, url)
    if vendor == "akamai-challenge":
        return "challenge"
    if vendor == "akamai" or status == 403:
        return "blocked"
    if status == 400:
        return "rejected"
    if status == 429:
        return "throttled"
    return "unknown"


# A served document is built out of the site's own assets and a refusal is
# not (§8): counted on 2026-09-28, `media.tractorsupply.com` occurs 92 times
# on the served homepage and dozens of times on each served /tsc/ page, and
# 0 times on every refusal.
SITE_ASSET_MARKER = "media.tractorsupply.com"


def detect_document_state(text: Optional[str], status: Optional[int] = None,
                          url: str = "") -> str:
    """Name what an HTML DOCUMENT is: the landing, or a category page.

        content   the site's own page
        blocked   Akamai's refusal, or a 403
        challenge Akamai's interactive challenge (not observed)
        missing   no such page: HTTP 404, or the site's own Next.js error
                  page. A slug that does not exist answers HTTP **500** with
                  `"page": "/_error"` (measured), so the status alone would
                  call it a server fault worth retrying.
        unknown   anything else, e.g. Chromium's own error page
    """
    vendor = detect_bot_challenge(text, url)
    if vendor == "akamai-challenge":
        return "challenge"
    if vendor == "akamai" or status == 403:
        return "blocked"
    if status == 404 or (next_data(text) or {}).get("page") == "/_error":
        return "missing"
    if status in (None, 200) and SITE_ASSET_MARKER in (text or ""):
        return "content"
    return "unknown"


def api_error(text: Optional[str]) -> Optional[str]:
    """The endpoint's own complaint, for a `rejected` response."""
    cat = catalog_record(text)
    if cat is not None and cat.get("error"):
        return "catalog error: %s" % str(cat["error"])[:300]
    payload = _json_or_none(text)
    if isinstance(payload, dict) and ("errorCode" in payload or "errorMessage" in payload):
        return "%s: %s" % (payload.get("errorCode", "error"),
                           payload.get("errorMessage") or "(no message)")
    return None


# ---------------------------------------------------------------------------
# Resolution: the ZIP's stores, the category's id
# ---------------------------------------------------------------------------

def parse_store_set(text: Any, zip_code: str) -> Optional[StoreSet]:
    """The nearby stores for a ZIP, or None when the site lists none.

    An empty `storesList` is the site's answer for a ZIP with no Tractor
    Supply nearby (00000 and Anchorage's 99501, both measured), and it has to
    stop the run: the search endpoint refuses a request naming no store.
    """
    payload = _json_or_none(text)
    if not isinstance(payload, dict):
        return None
    stores = [s for s in (payload.get("storesList") or [])
              if isinstance(s, dict) and str(s.get("stlocId") or "").strip()]
    if not stores:
        return None
    first = stores[0]
    zone = str(first.get("zoneNum") or "").strip()
    if not zone:
        return None
    return StoreSet(zip_code=zip_code,
                    store_ids=tuple(str(s["stlocId"]).strip()
                                    for s in stores[:STORES_PER_REQUEST]),
                    zone=zone,
                    state=_str(first.get("state")),
                    home_store_name=_str(first.get("storeName")))


_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def next_data(html: Optional[str]) -> Optional[Dict[str, Any]]:
    m = _NEXT_DATA_RE.search(html or "")
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _department_id(inner: Dict[str, Any]) -> Optional[str]:
    """A department's search id: the parent every one of its children names.

    A department page (/tsc/category/pet, and /tsc/catalog/farm-ranch, which
    redirects there) has no grid and no `catIdDetails`. Its
    `selectedEntry.value` LOOKS like an id and is not one: it is the
    catalogue identifier (1001811 for Farm & Ranch), and the search endpoint
    answers it with resultsFound 0 — a silent exit 4 on a department of 8,103
    products. The id the endpoint filters on is the one each child in
    `linkTreeData.catalogGroupView` names as its `parentCatalogGroupID`
    (26654 for Farm & Ranch, 439 for Pet; each child's own `uniqueID` is in
    turn the id its category page states, e.g. Dog = 236). Measured
    2026-09-28 on both departments.

    A child filed under several departments names all of them, each
    prefixed with a catalogue id: `['10051_26654', '10051_28042']` for
    Sprayers, against a bare `'26654'` for Tanks. So the prefix is stripped,
    and the department's id is the one EVERY child names — returned only
    when exactly one does, since a page whose children disagree is not one
    this code understands.
    """
    views = [v for v in ((inner.get("linkTreeData") or {}).get("catalogGroupView") or [])
             if isinstance(v, dict)]
    common = None
    for v in views:
        p = v.get("parentCatalogGroupID")
        mine = {re.sub(r"^\d+_", "", _str(x)) for x in (p if isinstance(p, list) else [p])
                if _str(x)}
        common = mine if common is None else (common & mine)
    return next(iter(common)) if common and len(common) == 1 else None


def category_details(html: Optional[str]) -> Optional[Dict[str, Optional[str]]]:
    """The listing's search id, name and path from its page, or None.

    A category page (/tsc/catalog/{slug}) keeps it at
    props.pageProps.pageProps.content.categoryDetails, and `catIdDetails.value`
    is the id the search endpoint filters on (26628 for 3-point-sprayers,
    which is also what the site's own grid carries as
    `data-cnstrc-filter-value`). A department page keeps `categoryDetails`
    one level up and has no such id; see `_department_id`.

    `seo_token_ntk` is the slug the page resolved to, which differs from the
    one asked for after a redirect (chicken-feed -> poultry-feed-treats,
    measured).
    """
    d = next_data(html)
    if not d:
        return None
    pp = (d.get("props") or {}).get("pageProps") or {}
    inner = pp.get("pageProps") if isinstance(pp.get("pageProps"), dict) else pp
    leaf = (inner.get("content") or {}).get("categoryDetails")
    if isinstance(leaf, dict):
        sel = leaf.get("selectedEntry") or {}
        cid = _str((leaf.get("catIdDetails") or {}).get("value")) or _str(sel.get("value"))
        kind = "category"
    else:
        dept = inner.get("categoryDetails") if isinstance(inner.get("categoryDetails"), dict) else {}
        sel = dept.get("selectedEntry") or {}
        cid = _department_id(inner) if sel else None
        kind = "department"
    if not cid:
        return None
    return {"id": cid, "name": _str(sel.get("name")), "kind": kind,
            "path": _str(sel.get("fullPath")), "slug": _str(sel.get("seo_token_ntk"))}


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------

def total_results(text: Any, mode: str = "") -> Optional[int]:
    """The site's own count of everything that matches (`resultsFound`)."""
    data = (catalog_record(text) or {}).get("data")
    if not isinstance(data, dict):
        return None
    t = _int((data.get("metaData") or {}).get("resultsFound"))
    return t if t is not None and t >= 0 else None


def pages_available(total: Optional[int], page_size: int) -> Optional[int]:
    if total is None:
        return None
    return max(1, math.ceil(total / page_size)) if total else 0


def count_rows(text: Any) -> int:
    return len(_entries(catalog_record(text)))


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def _float(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = html_lib.unescape(str(v)).strip()
    return s or None


def _attrs(entry: Dict[str, Any]) -> Dict[str, List[str]]:
    """`attributes` as identifier -> [value, ...]."""
    out: Dict[str, List[str]] = {}
    for a in entry.get("attributes") or []:
        if not isinstance(a, dict) or not a.get("identifier"):
            continue
        out[a["identifier"]] = [str(v.get("value")) for v in (a.get("values") or [])
                                if isinstance(v, dict) and v.get("value") is not None]
    return out


def product_url(entry: Dict[str, Any]) -> str:
    """The product's page. `seo_token_ntk` is the path's last segment: the
    same token the site's own JSON-LD `offers.url` is built from (compared on
    33 of 33 products of one archived category page)."""
    token = _str(entry.get("seo_token_ntk"))
    return "%s/tsc/product/%s" % (BASE, token) if token else ""


def image_url(entry: Dict[str, Any]) -> Optional[str]:
    t = _str(entry.get("thumbnail"))
    if t:
        return t
    x = _str(entry.get("xf_thumbnail"))
    return ("https://media.tractorsupply.com/is/image/TractorSupplyCompany/%s?$456$" % x
            if x else None)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def _inventory_index(inv_rec: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """productId (the item SKU) -> its availability record."""
    data = (inv_rec or {}).get("data") or {}
    resp = data.get("getInventoryAvailibilityResponse") or {}
    out = {}
    for a in resp.get("availabilityByProducts") or []:
        if isinstance(a, dict) and a.get("productId") is not None:
            out[str(a["productId"])] = a
    return out


def _atp(avail: Dict[str, Any], kind: str, location: Optional[str] = None) -> Optional[float]:
    """Units available to promise for one fulfilment type, or None if the
    record does not carry that type at all."""
    for t in avail.get("availabilityByFulfillmentTypes") or []:
        if not isinstance(t, dict) or t.get("fulfillmentType") != kind:
            continue
        total = 0.0
        for d in t.get("availabilityDetails") or []:
            if not isinstance(d, dict):
                continue
            if location is None:
                total += _float(d.get("atp")) or 0.0
            else:
                for loc in d.get("availabilityByLocations") or []:
                    if isinstance(loc, dict) and str(loc.get("locationId")) == location:
                        total += _float(loc.get("atp")) or 0.0
        return total
    return None


def _discount(price: Optional[float], was: Optional[float]) -> Optional[float]:
    if price is None or was is None or was <= 0 or price >= was:
        return None
    return round(100.0 * (was - price) / was, 1)


def _same(a: Optional[float], b: Optional[float]) -> bool:
    return a is not None and b is not None and abs(a - b) < 0.005


def parse_entry(entry: Dict[str, Any], query: Query, inventory: Dict[str, Dict[str, Any]],
                *, page: int, position: int) -> Optional[Product]:
    sku = _str(entry.get("partNumber"))
    title = _str(entry.get("name")) or _str(entry.get("shortDescription"))
    if not sku or not title:
        return None
    at = _attrs(entry)
    dp = entry.get("displayPrice") if isinstance(entry.get("displayPrice"), dict) else {}

    # `price` is NOT the field called `price`. On a multi-variant product the
    # endpoint's `price` is one variant's, and measured on 249 products it
    # equalled `offerPriceMin` on only 196: on the rest it was the MAXIMUM
    # (104.99 on a bag whose sizes start at 26.99). The listing tile shows a
    # range, so the row carries both ends and `price` is the low one.
    lo, hi = _float(entry.get("offerPriceMin")), _float(entry.get("offerPriceMax"))
    if lo is None:
        lo = _float(dp.get("offer_price_min"))
    if lo is None:
        lo = _float(entry.get("price"))
    if hi is None:
        hi = _float(dp.get("offer_price_max"))
    if hi is None:
        hi = lo

    # A struck-through list price appears on 25 of 249 products. It is a
    # "was" price for THIS price only when both are single values: on a
    # variant range the site's own list_price_min can sit below
    # offer_price_max, which would compute a discount nobody is offered.
    lmin, lmax = _float(dp.get("list_price_min")), _float(dp.get("list_price_max"))
    original = lmin if (_same(lo, hi) and _same(lmin, lmax) and lmin > lo) else None

    rating = _float(entry.get("xf_prdRating"))
    if rating is None:
        rating = _float((at.get("_BazaarVoiceReviewRating") or [None])[0])
    count = _int((at.get("_BazaarVoiceReviewCount") or [None])[0])
    # A product nobody has reviewed carries no rating attribute at all (40
    # of 249) rather than a zero. Guard the zero anyway (§21): a rating of 0
    # with no reviews is "unreviewed", not a grade.
    if not count and rating == 0.0:
        rating = None

    item_sku = _str(entry.get("defaultChildSKU"))
    avail = inventory.get(item_sku) if item_sku else None
    home = query.stores.home if query.stores else None
    in_stock = ship_ok = pick_ok = None
    if avail is not None:
        in_stock = str(avail.get("atpStatus") or "").upper() == "OK"
        s = _atp(avail, "SHIP")
        ship_ok = None if s is None else s > 0
        pk = _atp(avail, "PICK", home) if home else None
        pick_ok = None if pk is None else pk > 0

    ppm = entry.get("pricePerMeasurement") if isinstance(entry.get("pricePerMeasurement"), dict) else {}
    promos = [p for p in (entry.get("promotions") or []) if isinstance(p, dict)]
    ribbon = _str(dp.get("ribbon"))
    buyable = entry.get("buyable")
    return Product(
        url=product_url(entry),
        sku=sku,
        title=title,
        brand=_str(entry.get("manufacturer")),
        price=lo,
        # The endpoint states no currency, and needs none: every store it can
        # price against is a US store, named by a US ZIP (Query.validate).
        currency="USD" if lo is not None else None,
        original_price=original,
        discount_pct=_discount(lo, original),
        rating=round(rating, 2) if rating is not None else None,
        review_count=count,
        in_stock=in_stock,
        image_url=image_url(entry),
        category=query.category_path if query.mode == "category" else None,
        price_source="api",
        page=page,
        position=position,
        mode=query.mode,
        sort=query.sort,
        item_sku=item_sku,
        model_number=_str(entry.get("mfPartNumber_ntk")),
        price_max=hi,
        price_per_unit=_float(ppm.get("amount")),
        price_unit=_str(ppm.get("unit")),
        map_pricing=_str(dp.get("mapp_type")) if dp.get("is_Mapp") else None,
        badge=None if ribbon in (None, "NONE") else ribbon,
        promotion=_str(promos[0].get("message")) if promos else None,
        variant_count=len(entry.get("xf_sku") or []) or None,
        buyable=None if buyable is None else str(buyable).lower() == "true",
        sold_via=_str((at.get("_AvailabilityStatusOnly") or [None])[0]),
        ship_available=ship_ok,
        pickup_available=pick_ok,
        store_id=home,
        zip_code=query.stores.zip_code if query.stores else query.zip_code,
    )


def parse_page(text: Any, query: Query, page: int = 1) -> List[Product]:
    """Every product on one page, in the site's order.

    `position` counts the rows EMITTED, not the slots in the payload, so a
    record the parser drops cannot shift every later position (§24).
    """
    cat = catalog_record(text)
    if cat is None or cat.get("error"):
        return []
    inventory = _inventory_index(inventory_record(text))
    rows: List[Product] = []
    for e in _entries(cat):
        row = parse_entry(e, query, inventory, page=page, position=len(rows) + 1)
        if row:
            rows.append(row)
    return rows


def relevance_share(rows: List[Product], keyword: Optional[str]) -> Optional[float]:
    """Share of rows whose title or brand contains a word of the keyword.

    The endpoint answers a query it cannot match with 100 results of
    something else and says nothing about it ("qzxqzxvvv" -> Wrangler jeans,
    measured 2026-09-28). No field distinguishes that from a real answer, so
    the engine measures the rows instead and warns when none of them
    mentions the query. A warning, not a verdict: a search for a part number
    can legitimately return titles that never repeat it.
    """
    words = [w for w in re.findall(r"[a-z0-9]+", (keyword or "").lower()) if len(w) >= 3]
    if not rows or not words:
        return None

    def hit(r: Product) -> bool:
        hay = ("%s %s" % (r.title or "", r.brand or "")).lower()
        return any(w in hay or (w.endswith("s") and w[:-1] in hay) for w in words)
    return sum(1 for r in rows if hit(r)) / len(rows)
