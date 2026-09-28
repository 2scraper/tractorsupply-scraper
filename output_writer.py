"""
output_writer.py
-----------------
Row model + JSON/CSV writers shared by all three engines.

Both modes (`category`, `search`) produce the family's `Product` row. Its
first sixteen columns are byte-identical, in order, to every shop scraper in
this family (CLAUDE.md §9), so a consumer reading several of these repos
reads the same prefix. Everything tractorsupply-specific is appended after
`price_source`.
"""

import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# Every row comes from www.tractorsupply.com.
SOURCE_DEFAULT = "tractorsupply.com"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Product:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=_now)
    # The product page, /tsc/product/{seo token}.
    url: str = ""
    # `partNumber`, the PRODUCT's id (e.g. 135429599). Deliberately not the
    # item SKU that ends the product URL (1354295): a product with sizes has
    # one partNumber and up to 50 item SKUs (measured), and a listing row is
    # the product. The default item's SKU is `item_sku`.
    sku: Optional[str] = None
    title: Optional[str] = None
    # `manufacturer`, as the site states it.
    brand: Optional[str] = None
    # The LOW end of the product's price range at `store_id` (see
    # product_parser.parse_entry for why this is not the endpoint's `price`
    # field). Equal to `price_max` on a single-SKU product.
    price: Optional[float] = None
    currency: Optional[str] = None
    # The struck-through list price, only where both it and the price are
    # single values; null on a variant range, where a "was" figure would not
    # belong to the price beside it.
    original_price: Optional[float] = None
    discount_pct: Optional[float] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    # From the `oms_inventory` stream: atpStatus OK. Null when the stream does
    # not list the product, never guessed — which is every multi-variant
    # product: 0 of 108 measured had any of their variant SKUs in it.
    in_stock: Optional[bool] = None
    image_url: Optional[str] = None
    # The category's path as the site names it ("Farm & Ranch > Sprayers >
    # 3 Point Sprayers"); null in search mode, where the listing is not one.
    category: Optional[str] = None
    # Always "api": every row is read from the site's own search endpoint.
    # Kept because the family's diff_runs.py reads it.
    price_source: Optional[str] = None

    # ---- tractorsupply-specific, appended so the family prefix is stable ----
    page: Optional[int] = None
    position: Optional[int] = None
    mode: Optional[str] = None
    # The ordering the listing was requested in. Not cosmetic: a capped run
    # holds the first N products BY THIS KEY, so it decides which products are
    # in the file, and diff_runs.py refuses to compare two orderings.
    sort: Optional[str] = None
    # The default item's SKU, the number that ends the product URL.
    item_sku: Optional[str] = None
    # The manufacturer's part number (`mfPartNumber_ntk`).
    model_number: Optional[str] = None
    price_max: Optional[float] = None
    # "4.77 per lb" on a bag of feed: the site's own unit price.
    price_per_unit: Optional[float] = None
    price_unit: Optional[str] = None
    # Minimum-advertised-price handling, as the site names it
    # (CHECKOUT_ONLY_NOSTRIKE, ...): the price is shown in the cart rather
    # than on the page. 11 of 249 products measured. Null when not MAP-priced.
    map_pricing: Optional[str] = None
    # The tile ribbon: SALE or NEWARRIVAL; null for NONE.
    badge: Optional[str] = None
    # The first promotion's own text.
    promotion: Optional[str] = None
    variant_count: Optional[int] = None
    buyable: Optional[bool] = None
    # The sales channel the site states: "Online In Stores", "Online Only",
    # "In Stores Only". NOT stock; that is `in_stock`.
    sold_via: Optional[str] = None
    # Whether the default SKU can ship / be picked up at `store_id` today,
    # from the inventory stream. Null where the stream says nothing.
    ship_available: Optional[bool] = None
    pickup_available: Optional[bool] = None
    # The store the prices are for, and the ZIP it was chosen from.
    store_id: Optional[str] = None
    zip_code: Optional[str] = None


# Row classes by --mode, so an engine maps its mode to a schema in one place.
ROW_CLASS_BY_MODE = {"category": Product, "search": Product}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku`
# and to hand to diff_runs.py.
UNIQUE_BY_SKU_MODES = ("category", "search")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages.

    A listing ordered by popularity can move between two page fetches, and
    a product that moves across a page boundary is fetched twice (dropped
    here) or not at all (invisible to any scraper). The README says so.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked before any data arrived: Akamai's Access
# Denied, or any 403. Distinct from EXIT_NO_PRODUCTS so a caller can tell
# "the listing genuinely has nothing in it" from "something stood between us
# and the listing".
#
# An empty listing is NOT this code. A category id with nothing in it answers
# HTTP 200 with `resultsFound: 0`, and that is EXIT_NO_PRODUCTS: the request
# was served exactly as asked.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because every engine returns it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


# Exit code for a run that never GOT its pages: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Distinct from EXIT_NO_PRODUCTS because those are opposite facts. Exit 4 is
# a statement about the CATALOGUE — "we asked, and the answer was nothing" —
# so handing it to a run that never reached the site tells a pipeline the
# listing is empty when nothing was read at all.
#
# 5 rather than a new number, and 5 rather than EXIT_PARTIAL:
#
#   * this family's contract already reserves 5 for a transport failure
#     (a remote API error), so this needs no new code and no per-repo table for a caller
#     driving more than one of these scrapers;
#   * EXIT_PARTIAL (6) means "some rows were gathered and the output is
#     incomplete". A run holding nothing writes no output at all, so a
#     consumer that reads the file on a 6 finds either nothing or the
#     PREVIOUS run's good data, which `save` deliberately does not
#     overwrite. Exit 5 promises no file.
#
# Deliberately NOT applied when rows WERE gathered: a timeout on page 7 of
# 10 is a partial run (exit 6, output written), which is already right. This
# decides only what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "listing", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are recorded because `mode` is not implied by the
    repo: one output prefix can hold a category run or a search run.
    diff_runs.py refuses a pair whose modes or sources differ.

    `extra` carries facts about the run that are not about any single row:
    the query that was sent (the listing, its ordering, the ZIP and the
    stores it priced against) and the site's OWN count of what matched
    (`total_results`, `pages_available`). The count is the only honest way
    to say how much of a listing a run holds: a 3-page search is complete as
    a REQUEST and a 144-of-1,076 sample as a LISTING, and only the sidecar
    can say so.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 rows -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 rows — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} rows -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} rows -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" is kept for the
# engines that still stop on a missing next-link: the first is a property of
# the DATA (a page contributed nothing not already seen, so the listing is
# over), while the second is a property of a CSS SELECTOR and is therefore
# the weaker signal — a renamed attribute looks identical to a short
# catalogue.
#
# On this site there is a third and stronger signal, the site's own
# arithmetic. Every listing states its total on page 1, so the number of
# pages is PLANNED rather than discovered, and a run that fetched them all
# ends "completed". "end_of_listing" is the data-side stop: a page came back
# empty, meaning the live listing shrank below the plan during the run.
# That is complete too, because there was nothing more to get.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "end_of_listing")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "listing", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    # Completeness is decided by the reason AND by the evidence. A named
    # list of stop reasons cannot cover a failure recorded somewhere else,
    # and `pages_failed` is somewhere else: a run whose loop ended for a
    # COMPLETE reason while individual pages failed reported exit 0 and
    # `status: complete` with a non-empty `pages_failed` in the same
    # sidecar — a file that contradicts itself, and a pipeline branching
    # on `status` reading a short run as a whole one.
    #
    # Found by a third-party audit of a sibling repo and measured across
    # the family by CALLING each `finish_run` rather than grepping for the
    # fix: 28 of 32 repos behaved this way. Same shape as the exit-code
    # unification this file already carries — a rule keyed on a list of
    # names has a hole for every name nobody added to it.
    complete = stop_reason in COMPLETE_STOP_REASONS and not pages_failed
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are different facts and a pipeline branches on them
        # (blocked is not empty is not "never reached"):
        #
        #   blocked            something stood between the run and the content
        #   did not complete   we never got the pages — a dead proxy, a load
        #                      timeout, an edge serving something else
        #   completed          we asked, and the answer was nothing
        #
        # Keyed on `not complete` rather than on a list of stop reasons, on
        # purpose: a list cannot cover a reason nobody has added to it yet,
        # so a new one falls silently through to "the catalogue is empty" —
        # which is the defect this branch exists to prevent.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Nothing was gathered and the run did not finish "
                  f"({stop_reason}) — exit {EXIT_FETCH_FAILED}, NOT an empty "
                  f"result (exit {EXIT_NO_PRODUCTS}). Nothing can be "
                  f"concluded about the catalogue from this run.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
