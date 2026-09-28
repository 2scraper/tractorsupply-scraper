#!/usr/bin/env python3
"""Cut the offline suite's fixtures out of real captures, and PROVE they parse
the same.

Its output is `fixtures_generated.json`, which `smoke_test.py` loads.

WHY A GENERATED FILE RATHER THAN INLINE LITERALS
------------------------------------------------
The family's rule is fixtures inline in `smoke_test.py`. Here one search
response is 100-300 KB of newline-delimited JSON and one category page is
550 KB of HTML. Pasting those into a Python source file would make the suite
unreadable, so the fixtures are CUT down by this script, written beside it,
and checked by the suite for the same properties inline fixtures would be.
Sibling repos (binance, rakuten, polymarket) do the same for the same reason.

WHAT YOU NEED TO RUN IT
-----------------------
Your own captures, in `captures/` (ignored by git), named as the SOURCES
table below expects. Take them with `--dump-html`, which writes exactly the
response the parser was given.

WHAT IT ENFORCES
----------------
  * every fixture is CUT from a real capture, never hand-written:
      - a search response keeps its own records, with only the products
        listed in `keep` (and their inventory) — every value verbatim;
      - a page keeps only the __NEXT_DATA__ paths the parser reads, and one
        real asset tag from the same page, because a served document is
        recognised by its own assets (product_parser.SITE_ASSET_MARKER);
      - a refusal is kept verbatim: it is a few hundred bytes, and its exact
        spelling (entity-escaped or not) is what the checks are about;
  * each one parses IDENTICALLY to the untrimmed original: the same state,
    the same total, the same rows for the products it keeps, the same
    category id, the same stores;
  * nothing session-shaped survives. A captured page echoes Akamai's and
    analytics vendors' cookie values, a Riskified session id and an Adobe
    visitor id into its __NEXT_DATA__; none of those paths is kept, and a
    fixture holding a 32-hex string or any of those names is refused.

    python3 make_fixtures.py [captures_dir]
"""
import json
import pathlib
import re
import sys
from dataclasses import asdict

REPO = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

import product_parser as pp  # noqa: E402

HEX32 = re.compile(r"\b[0-9a-f]{32}\b")
SESSION_SHAPES = re.compile(r"riskified|adobe_mc|MCMID|_abck|bm_sz|ak_bmsc|"
                            r"sessionId|\"guid\"|koddi|akacd", re.I)

GA = {"store_ids": ["2293", "568", "1581", "1964", "1131", "2737"], "zone": "23",
      "state": None, "zip_code": "30013"}
TX = {"store_ids": ["2661", "539", "444", "2379", "2209"], "zone": "13",
      "state": None, "zip_code": "75001"}
CAT_3PT = {"mode": "category", "slug": "3-point-sprayers", "category_id": "26628",
           "category_path": "Farm & Ranch > Sprayers > 3 Point Sprayers"}
KW_DOG = {"mode": "search", "keyword": "dog food"}
KW_JUNK = {"mode": "search", "keyword": "qzxqzxvvv"}

# name -> (capture, kind, query, stores, keep)
#   kind   search | json | page | verbatim
#   keep   for a search response: indexes into catalogEntryView, chosen for
#          the case each product is (named in the comment); None keeps all.
SOURCES = {
    # 3 Point Sprayers, Georgia stores, 24 a page. Kept: a single-SKU
    # product with a struck list price (0), an "In Stores Only" product the
    # inventory marks UNAVAILABLE (1), an unreviewed one (4), another
    # unavailable one (22).
    "cat_p1": ("search_cat_3pt_ga_p1.ndjson", "search", CAT_3PT, GA, [0, 1, 4, 22]),
    # Page 2 of the same: 13 products, kept 3, one of them not buyable (1).
    "cat_p2": ("search_cat_3pt_ga_p2.ndjson", "search", CAT_3PT, GA, [0, 1, 5]),
    "cat_past_end": ("search_cat_3pt_ga_p99.ndjson", "search", CAT_3PT, GA, None),
    # The same category priced at two store sets: kept, the one product in
    # 37 whose price differs (215226999: 1799.99 in GA, 1899.99 in TX).
    "price_ga": ("search_cat_3pt_ga_all.ndjson", "search", CAT_3PT, GA, "215226999"),
    "price_tx": ("search_cat_3pt_tx_all.ndjson", "search", CAT_3PT, TX, "215226999"),
    "cat_unknown_id": ("search_cat_unknown_id.ndjson", "search", CAT_3PT, GA, None),
    # "dog food": a multi-variant product (0), one whose `price` is not its
    # low end (2), a MAP-priced one (4), a single-SKU one with a list price
    # (22).
    "kw_p1": ("search_kw_dogfood_p1.ndjson", "search", KW_DOG, GA, [0, 2, 4, 22]),
    "kw_junk": ("search_kw_junk_p1.ndjson", "search", KW_JUNK, GA, [0, 1, 2, 3]),
    "rejected_zone": ("search_rejected_zone1.ndjson", "search", CAT_3PT, GA, None),
    "rejected_sort": ("search_rejected_sort9.json", "verbatim", None, None, None),
    "rejected_page_size": ("search_rejected_pagesize500.json", "verbatim", None, None, None),
    "rejected_no_channel": ("search_rejected_no_channel.json", "verbatim", None, None, None),
    "rejected_no_store": ("search_rejected_no_store.json", "verbatim", None, None, None),
    "stores_75001": ("stores_75001.json", "stores", None, None, None),
    "stores_99501": ("stores_99501.json", "verbatim", None, None, None),
    "page_category": ("page_category_3pt.html", "page", None, None, None),
    "page_department_farm_ranch": ("page_department_farm_ranch.html", "page", None, None, None),
    "page_department_pet": ("page_department_pet.html", "page", None, None, None),
    "page_missing": ("page_missing_500.html", "page", None, None, None),
    "akamai_raw": ("akamai_deny_raw_curl.html", "verbatim", None, None, None),
    "akamai_dom_cdp": ("akamai_deny_dom_cdp.html", "verbatim", None, None, None),
    "landing_cdp": ("landing_home_cdp.html", "landing", None, None, None),
}

# The HTTP status each capture was answered with, recorded because the
# classifiers take it and it is not in the bytes.
STATUS = {"rejected_sort": 400, "rejected_page_size": 400, "rejected_no_channel": 400,
          "rejected_no_store": 400, "page_missing": 500, "akamai_raw": 403,
          "akamai_dom_cdp": 403}


def query_for(spec, stores):
    q = pp.Query(**{k: v for k, v in (spec or {"mode": "category", "slug": "x"}).items()})
    if stores:
        q.stores = pp.StoreSet(zip_code=stores["zip_code"], store_ids=tuple(stores["store_ids"]),
                               zone=stores["zone"], state=stores["state"])
    return q


def rows_json(rows):
    out = []
    for r in rows:
        d = asdict(r)
        d.pop("scraped_at")
        out.append(d)
    return out


def cut_search(text, keep):
    recs = pp.ndjson_records(text)
    out = []
    kept_items = set()
    for rec in recs:
        rec = json.loads(json.dumps(rec))
        if rec.get("type") == "catalog" and isinstance(rec.get("data"), dict):
            data = rec["data"]
            ents = data.get("catalogEntryView") or []
            if isinstance(keep, str):
                ents = [e for e in ents if e.get("partNumber") == keep]
            elif keep is not None:
                ents = [ents[i] for i in keep]
            # `most_attractive_seo_tokens` is a list of colour variants'
            # URL slugs under the key `seoToken`, which the family's
            # credential scan reads as a token field. The parser never reads
            # it, so it is dropped rather than exempted (CLAUDE.md §24:
            # "before adding an exemption, check whether the value is
            # needed at all").
            for e in ents:
                e.pop("most_attractive_seo_tokens", None)
            data["catalogEntryView"] = ents
            kept_items = {str(e.get("defaultChildSKU")) for e in ents}
            # Facets and breadcrumbs are hundreds of KB the parser never
            # reads; the metadata (resultsFound) is kept whole.
            for k in ("facetView", "breadCrumbTrailEntryView",
                      "breadCrumbTrailEntryViewExtended", "relatedSearches"):
                if k in data:
                    data[k] = []
        out.append(rec)
    for rec in out:
        if rec.get("type") == "oms_inventory":
            resp = ((rec.get("data") or {}).get("getInventoryAvailibilityResponse") or {})
            if "availabilityByProducts" in resp:
                resp["availabilityByProducts"] = [
                    a for a in resp["availabilityByProducts"]
                    if str(a.get("productId")) in kept_items]
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in out) + "\n"


def first_asset_tag(html):
    m = re.search(r'<(?:img|link)[^>]*media\.tractorsupply\.com[^>]*>', html)
    if not m:
        raise SystemExit("no media.tractorsupply.com tag to keep")
    return m.group(0)


def cut_page(html):
    nd = pp.next_data(html)
    pp_outer = nd["props"]["pageProps"]
    inner = pp_outer.get("pageProps") if isinstance(pp_outer.get("pageProps"), dict) else pp_outer
    keep_inner = {}
    content = inner.get("content") or {}
    if "categoryDetails" in content:
        keep_inner["content"] = {"categoryDetails": content["categoryDetails"]}
    if "categoryDetails" in inner:
        keep_inner["categoryDetails"] = inner["categoryDetails"]
    views = (inner.get("linkTreeData") or {}).get("catalogGroupView")
    if views:
        keep_inner["linkTreeData"] = {"catalogGroupView": [
            {k: v.get(k) for k in ("identifier", "uniqueID", "name", "parentCatalogGroupID")}
            for v in views]}
    pruned = {"page": nd.get("page"), "props": {"pageProps": {"pageProps": keep_inner}}}
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S)
    return ("<!DOCTYPE html><html><head><title>%s</title></head><body>%s"
            '<script id="__NEXT_DATA__" type="application/json">%s</script>'
            "</body></html>" % (title.group(1) if title else "", first_asset_tag(html),
                                json.dumps(pruned, ensure_ascii=False)))


def cut_landing(html):
    """The CDP-served homepage: its title, one asset tag, and every tag the
    Scraping Browser's auto-solve extension injected — which is the point of
    this fixture (§24: a marker set must score zero on a page fetched the
    way a real run fetches it)."""
    injected = re.findall(r"<script[^>]*chrome-extension://[^>]*>\s*</script>", html)
    widgets = re.findall(r"<captcha-widgets[^>]*>.*?</captcha-widgets>", html, re.S)
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S)
    return ("<!DOCTYPE html><html><head><title>%s</title>%s</head><body>%s%s</body></html>"
            % (title.group(1) if title else "", "".join(injected), first_asset_tag(html),
               "".join(widgets)))


def cut_stores(text):
    d = json.loads(text)
    d["storesList"] = [{k: s.get(k) for k in ("stlocId", "zoneNum", "state", "storeName",
                                              "city", "zipCode")}
                       for s in d["storesList"][:pp.STORES_PER_REQUEST]]
    return json.dumps(d, ensure_ascii=False)


def verify(name, kind, orig, cut, spec, stores):
    status = STATUS.get(name)
    if kind in ("search", "verbatim") and kind != "stores":
        a, b = pp.detect_page_state(orig, status), pp.detect_page_state(cut, status)
        assert a == b, (name, "state", a, b)
        assert pp.total_results(orig) == pp.total_results(cut), (name, "total")
        assert pp.api_error(orig) == pp.api_error(cut), (name, "api_error")
    if kind == "search":
        q = query_for(spec, stores)
        full = rows_json(pp.parse_page(orig, q, 1))
        mine = rows_json(pp.parse_page(cut, q, 1))
        skus = [r["sku"] for r in mine]
        want = [r for r in full if r["sku"] in skus]
        # position counts EMITTED rows (§24), so it is the one column a cut
        # legitimately renumbers.
        for r in want + mine:
            r.pop("position")
        assert mine == want, (name, "rows differ")
    if kind in ("page", "landing", "verbatim"):
        a, b = pp.detect_document_state(orig, status), pp.detect_document_state(cut, status)
        assert a == b, (name, "document state", a, b)
        assert pp.detect_bot_challenge(orig) == pp.detect_bot_challenge(cut), (name, "vendor")
    if kind == "page":
        assert pp.category_details(orig) == pp.category_details(cut), (name, "category")
    if kind == "stores":
        a, b = pp.parse_store_set(orig, "75001"), pp.parse_store_set(cut, "75001")
        assert a == b, (name, "stores")


def main():
    src = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else REPO / "captures")
    out = {}
    for name, (fname, kind, spec, stores, keep) in SOURCES.items():
        orig = (src / fname).read_text(encoding="utf-8")
        cut = {"search": lambda: cut_search(orig, keep), "page": lambda: cut_page(orig),
               "landing": lambda: cut_landing(orig), "stores": lambda: cut_stores(orig),
               "verbatim": lambda: orig, "json": lambda: orig}[kind]()
        verify(name, kind, orig, cut, spec, stores)
        bad = HEX32.findall(cut) + SESSION_SHAPES.findall(cut)
        if bad:
            raise SystemExit("%s: the cut still holds %s — refusing to write it"
                             % (name, sorted(set(bad))[:5]))
        out[name] = {"source": fname, "kind": kind, "status": STATUS.get(name),
                     "query": spec, "stores": stores, "text": cut}
        print("%-28s %8d -> %7d bytes" % (name, len(orig), len(cut)))
    path = REPO / "fixtures_generated.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")
    print("wrote %s (%d fixtures)" % (path.name, len(out)))


if __name__ == "__main__":
    main()
