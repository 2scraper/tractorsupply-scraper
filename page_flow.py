"""
page_flow.py
------------
The retry / blocked decision, as DATA rather than as three copies of an
if-chain (CLAUDE.md §1), and the one fetch loop all three engines share.

tractorsupply.com's search endpoint answers one of this repo's requests in
six ways, and they want five different responses:

    a `catalog` record with products                    -> parse
    a `catalog` record with none                        -> parse, it is an answer
    HTTP 400 `errorCode`, or a `catalog` record that
      carries `error` under HTTP 200                    -> stop: the PARAMETERS
                                                           were refused, and a
                                                           retry sends them again
    Akamai "Access Denied", or any 403                  -> rotate, or re-fetch
                                                           once from a fresh
                                                           browser
    429                                                 -> wait, same exit
    anything else                                       -> retry

Three copies of that triage across three engines would drift, and the drift
would be silent: one engine reporting exit 3 where its twin reports exit 0
on the same response.

Nothing here imports a browser, and **no JavaScript crosses this boundary**
(§1). Each engine spells its fetch() in its own driver's dialect.

There is no captcha path. Every refusal this repo met on tractorsupply.com
(curl, Chromium headless and headful, a residential exit, the Scraper API,
the Scraping Browser navigating) was Akamai's plain Access Denied page, with
no widget on it: nothing a solver could be paid to answer. See the README.
"""

import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from output_writer import dedupe_by_key, finish_run, SOURCE_DEFAULT
from product_parser import (DEFAULT_PAGE_SIZE, DEFAULT_SORT, DEFAULT_ZIP,
                            MAX_PAGES, ORIGIN_URL, Query, api_error,
                            category_details, category_page_request,
                            detect_document_state, detect_page_state,
                            pages_available, parse_page, parse_store_set,
                            query_from_url, relevance_share, request_for,
                            store_request, total_results)

log = logging.getLogger("page_flow")


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

# How long one fetch() may take before the engine gives up on it. The
# largest response measured was 315 KB (48 search results with their
# inventory), which arrived in about a second. The bound exists because a
# browser fetch() has no timeout of its own, and §8 requires every remote
# call to have one.
FETCH_TIMEOUT_MS = 45_000

# How long to wait at the SAME exit after a 429 before trying again. Not
# observed on this site; the status's own meaning is "slow down", which a
# rotation does not answer.
THROTTLE_WAIT_S = 10.0
THROTTLE_RETRIES = 2

# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "", waf_action: Optional[str] = None) -> str:
    """Name what the search endpoint answered with. See
    product_parser.detect_page_state.

    The argument ORDER is the contract: every engine calls
    `classify(html, status, url, waf_action)`. A sibling repo shipped
    `classify(html, url=...)` in two of three engines against a callee that
    took `status` second, and both crashed on their first fetch (§17).
    `smoke_test.py` binds every engine's call against this signature.
    """
    return detect_page_state(html or "", status, url, waf_action)


STATE_POLICY = {
    "content":    {"retry": False, "blocked": False, "parse": True},
    # A listing with nothing in it: an unknown category id, or a page past
    # the end. The site served exactly what was asked, so this is
    # EXIT_NO_PRODUCTS rather than EXIT_BLOCKED.
    "empty":      {"retry": False, "blocked": False, "parse": True},
    # The endpoint refused the PARAMETERS ("Invalid Input storeNumber",
    # "Max pageSize 200.", or a NullPointerException for a wrong zone). The
    # same request sent again gets the same answer and no exit changes it,
    # so nothing retries and nothing counts as blocked.
    "rejected":   {"retry": False, "blocked": False, "parse": False},
    # Akamai's interactive challenge. NOT OBSERVED here (product_parser).
    # A fresh browser may pass it, hence retry; there is no widget in it to
    # solve, and it counts as blocked if it persists.
    "challenge":  {"retry": True,  "blocked": True,  "parse": False},
    # 429. Not observed. The retry happens at the same exit after a wait
    # (THROTTLE_*), and it is NOT counted as blocked: calling a throttle a
    # block reports exit 3 for a page that was about to come back (§24).
    "throttled":  {"retry": True,  "blocked": False, "parse": False},
    # Akamai's Access Denied. It was the answer to every client but one
    # (module docstring of product_parser), so a re-fetch from the same
    # client is one attempt, and a different exit is what changes it.
    "blocked":    {"retry": True,  "blocked": True,  "parse": False},
    # HTTP 404 on a category PAGE: no such category. Not retried, and a
    # usage error rather than a refusal (resolve_query).
    "missing":    {"retry": False, "blocked": False, "parse": False},
    # Not a search response and not a refusal. Worth one more try.
    "unknown":    {"retry": True,  "blocked": False, "parse": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["retry"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["blocked"]


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["parse"]


# Whether a blocked page is worth re-fetching at all. CONSULTED by the loop
# below, so setting it False really does stop the retry (§17).
RETRY_ON_BLOCKED = True

# How many times to re-fetch a refused page when there is no proxy pool to
# rotate into. One: on this site a refusal was decided by the client and the
# address, and the same client from the same address got the same answer
# every time it was asked (7 of 7 /tsc/ fetches from a residential headful
# session, 2026-09-28). WITH a pool the loop retries once per remaining exit
# instead, because there the retry changes the variable the refusal depends
# on.
BLOCK_RETRIES_WITHOUT_POOL = 1


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def pages_to_plan(pages_requested: int, pages_available: Optional[int]) -> int:
    """How many pages a run may ask for, given the total page 1 reported.

    The endpoint states `resultsFound` on every page, so the end is known up
    front rather than discovered by walking off it. Walking off it is
    harmless here (a page past the end is an honest empty, measured) but
    costs a request per worker.
    """
    ceiling = MAX_PAGES if pages_available is None else min(pages_available, MAX_PAGES)
    return max(1, min(int(pages_requested), ceiling))


def concurrency_limit(cdp_endpoint: Optional[str]) -> Optional[int]:
    """1 when workers would collide, else None for "no limit imposed here".

    The Scraping Browser API allows ONE live connection per profile, so N
    workers sharing a `pid` collide with `profile_locked`. Several `pid`s,
    one run each, is the way to parallelise that path (§7).
    """
    return 1 if cdp_endpoint else None


# ---------------------------------------------------------------------------
# Refusals: how they are named and what the reader is told
# ---------------------------------------------------------------------------

def refusal_name(state: str) -> str:
    """The name a refusal is reported by, in logs and in `stop_reason`."""
    return {"challenge": "akamai-challenge", "blocked": "akamai"}.get(state, state)


def refusal_advice(state: str) -> str:
    """One sentence on what changes the answer. Kept here so the three
    engines cannot give three different pieces of advice."""
    return ("Akamai refused this client. In testing (2026-09-28) the only "
            "client it served was the Scraping Browser with a US exit "
            "(--cdp-endpoint, country-us); local Chromium was refused from a "
            "datacentre and from a US residential proxy alike. See the "
            "README's access table.")


def stop_reason_for(outcome) -> str:
    """The run's stop_reason when `outcome` is the page that ended it."""
    if getattr(outcome, "rejected", None):
        return "api_rejected"
    if getattr(outcome, "blocked_by", None):
        return "blocked_%s" % outcome.blocked_by
    if getattr(outcome, "state", None) == "throttled":
        return "throttled"
    return "page_load_timeout"


# ---------------------------------------------------------------------------
# The query, and the end of a run
# ---------------------------------------------------------------------------

# The listing flags, by the argparse dest they land in. A flag left at None
# was not typed, which is how build_query tells a user's value from a
# default.
LISTING_FLAGS = (("category", "--category"), ("search", "--search"))


def build_query(args, error: Callable[[str], None]) -> Query:
    """The Query a run sends, from --url or from the flags, validated.

    --url and --category/--search are two ways to name the listing. Typing
    both would scrape something neither of them named if they disagreed, so
    the combination is refused rather than merged (the family's --country
    rule, §10). `error` is argparse's `p.error`, so a refusal is exit 2 with
    the usage line, as in every engine.
    """
    typed = [flag for dest, flag in LISTING_FLAGS if getattr(args, dest, None)]
    if typed and args.url and getattr(args, "url_from_env", False):
        # TRACTORSUPPLY_URL is a default, and a flag the user typed beats a
        # default (§3's precedence) rather than colliding with it.
        log.info("Ignoring TRACTORSUPPLY_URL: %s names the listing.", typed[0])
        args.url = None
    if args.url:
        query, why = query_from_url(args.url)
        if query is None:
            error(why)
        if typed:
            error("--url already names the listing; %s would have to agree "
                  "with it and nothing checks that they do. Pass one of them."
                  % ", ".join(typed))
    elif len(typed) > 1:
        error("--category and --search name two different listings; pass one.")
    elif getattr(args, "category", None):
        query = Query(mode="category", slug=args.category.strip().strip("/").lower())
    elif getattr(args, "search", None):
        query = Query(mode="search", keyword=args.search.strip())
    else:
        error("name a listing: --url, --category SLUG or --search KEYWORD")
    if args.mode and args.mode != query.mode:
        error("--mode %s disagrees with the listing, which is a %s."
              % (args.mode, query.mode))
    query.sort = args.sort or DEFAULT_SORT
    query.page_size = args.page_size or DEFAULT_PAGE_SIZE
    query.zip_code = (args.zip or DEFAULT_ZIP).strip()
    why = query.validate()
    if why:
        error(why)
    if args.pages < 1:
        error("--pages must be at least 1")
    return query


def query_summary(query: Query) -> dict:
    """The query as the sidecar records it."""
    s = query.stores
    out = {"sort": query.sort, "page_size": query.page_size,
           "zip_code": query.zip_code,
           "store_ids": list(s.store_ids) if s else None,
           "zone": s.zone if s else None, "state": s.state if s else None,
           "home_store": s.home_store_name if s else None}
    if query.mode == "category":
        out.update({"category_slug": query.slug, "category_id": query.category_id,
                    "category_name": query.category_name,
                    "category_path": query.category_path})
    else:
        out["keyword"] = query.keyword
    return out


def finish(args, query: Query, outcomes: List, stop_reason: str,
           blocked: bool, extra: Optional[dict] = None) -> int:
    """Merge the pages in PAGE order, write the output, return the exit code.

    One implementation for the three engines, so the merge order, the
    dedupe and the sidecar cannot differ between them (§6).
    """
    rows, seen = [], set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, seen, key="sku")
        if len(fresh) < len(oc.products):
            log.info("Page %d: dropped %d duplicate row(s) — the listing moved "
                     "between page fetches.", oc.page_num,
                     len(oc.products) - len(fresh))
        rows.extend(fresh)

    first = next((o for o in outcomes if o.page_num == 1), None)
    total = getattr(first, "total_available", None)
    available = getattr(first, "pages_available", None)
    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = sorted(o.page_num for o in outcomes if not o.ok)
    if rows and total:
        log.info("The site reports %d match(es); this run holds %d (%.1f%%).",
                 total, len(rows), 100.0 * len(rows) / total)
    last_ok = max([o.page_num for o in ok_pages] or [1])
    meta = {"total_results": total, "pages_available": available,
            "query": query_summary(query)}
    meta.update(extra or {})
    return finish_run(
        rows, args.out, args.format, args.allow_empty,
        blocked=blocked, stop_reason=stop_reason,
        pages_requested=args.pages, pages_completed=len(ok_pages),
        pages_failed=failed_pages, mode=query.mode, source=SOURCE_DEFAULT,
        start_url=args.url or query.page_url,
        final_url=request_for(query, last_ok).url,
        extra=meta)


# ---------------------------------------------------------------------------
# The fetch loop, driven through named operations
# ---------------------------------------------------------------------------
#
# Everything about fetching one page lives here, once: landing on the
# origin, resolving the query, retrying a transport failure, waiting out a
# throttle, rotating on a refusal, and parsing what came back. The three
# engines differ only in HOW they ask their driver, so each passes in an
# object with these operations and no JavaScript crosses this boundary (§1):
#
#     ops.goto(url)        -> (status, waf_header). Raises TransportError.
#     ops.document_text()  -> the current document's markup
#     ops.wait_ms(ms)
#     ops.fetch(req)       -> (status, text, waf_header, error_or_None)
#     ops.relaunch()       -> a fresh browser (on the pool's current exit)
#     ops.landed           -> bool attribute, owned by the loop
#     ops.proxy_failure(text) -> the driver's proxy-error name in text, or ""
#
# One copy is how the family rule that the three engines agree on exit codes
# (§6) holds by construction rather than by discipline.


class TransportError(Exception):
    """A navigation that did not complete: a timeout, a dead proxy."""


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards, in page order, rather than
    folded into shared state as the loop goes, so the output cannot depend
    on which page happened to finish first (§8).
    """
    page_num: int
    url: str
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    # The state the page came back as. Carried so the caller can tell an
    # EMPTY page (the end of the listing) from a failed one: both hold zero
    # rows and they mean opposite things.
    state: Optional[str] = None
    # The endpoint's own complaint when it refused the parameters.
    rejected: Optional[str] = None
    # The site's own count of what matched, from this page's response.
    total_available: Optional[int] = None
    pages_available: Optional[int] = None

    @property
    def ok(self) -> bool:
        return (not self.load_failed and self.blocked_by is None
                and self.rejected is None)


# The columns the endpoint filled on EVERY record of every capture (249
# products, 2026-09-28). Below this share, the payload shape has moved rather
# than the data being unusual. Deliberately NOT here: `rating` (40 of 249
# unreviewed), `original_price` (25 of 249), `in_stock` (null on every
# product the inventory stream does not list).
CORE_FIELD_FLOOR = 99
CORE_FIELDS = ("sku", "title", "url", "price", "brand", "image_url")


def land(ops, args) -> Tuple[str, Optional[str]]:
    """Put the page on a www.tractorsupply.com document fetch() can use.

    Returns (state, error): state is a document state for the landing
    (product_parser.detect_document_state), error a transport failure's text
    or None.
    """
    try:
        status, _waf = ops.goto(ORIGIN_URL)
    except TransportError as e:
        return "load_failed", str(e)
    state = detect_document_state(ops.document_text(), status, ORIGIN_URL)
    ops.landed = state == "content"
    return state, None


def _core_field_warnings(rows: List, page_num: int) -> None:
    for name in CORE_FIELDS:
        if not rows:
            return
        filled = sum(1 for r in rows if getattr(r, name, None) not in (None, "", []))
        share = 100.0 * filled / len(rows)
        if share < CORE_FIELD_FLOOR:
            log.warning("Only %.0f%% of page %d carries `%s`, against a "
                        "measured floor of %d%%. Every record of every capture "
                        "had one, so the payload shape has moved — re-run with "
                        "--dump-html.", share, page_num, name, CORE_FIELD_FLOOR)


def _dump(args, page_num: int, text: str) -> None:
    """Write the exact response the parser was given, on success too (§9)."""
    if not args.dump_html:
        return
    path = args.dump_html if args.pages == 1 else f"{args.dump_html}.page{page_num}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    log.info("Saved the response the parser sees to %s (%d bytes).",
             path, len(text))


def _save_debug(args, page_num: int, text: str) -> str:
    path = f"{args.out}_page{page_num}_debug.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text or "")
    return path


def _block_budget(args, pool) -> Tuple[bool, int]:
    has_pool = bool(pool and len(pool) > 1)
    budget = 0 if not RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool else BLOCK_RETRIES_WITHOUT_POOL)
    return has_pool, budget


def _rotate(ops, pool, has_pool: bool, why: str, attempt: int, budget: int,
            mask: Callable[[str], str]) -> None:
    if has_pool:
        log.warning("%s at %s — rotating to another exit (%d/%d).", why,
                    mask(pool.current), attempt + 1, budget)
        pool.advance(why)
    else:
        log.warning("%s — re-fetching once from a fresh browser.", why)
    ops.relaunch()


def fetch_one_page(ops, args, pool, query: Query, page_num: int,
                   mask: Callable[[str], str] = lambda s: s,
                   req=None, classify_fn=None) -> Tuple[PageOutcome, str]:
    """Fetch one request with the loop's retries and rotations.

    Returns (outcome, text): the outcome carries the state and any failure,
    the text is the last response. Never raises for an EXPECTED failure: a
    timeout, a refusal and a dead exit are all recorded on the outcome,
    because what the run should do about them differs between the
    sequential and the concurrent paths.

    `req` and `classify_fn` default to the search page and its classifier;
    the query-resolution requests pass their own.
    """
    req = req or request_for(query, page_num)
    classify_fn = classify_fn or classify
    outcome = PageOutcome(page_num=page_num, url=req.label)
    has_pool, block_retries = _block_budget(args, pool)
    throttles = 0
    state, text, last_error, exit_failed = "unknown", "", None, None
    at_landing = False

    for block_attempt in range(block_retries + 1):
        log.info("Fetching %s", req.label)
        exit_failed = None
        attempt = 0
        while attempt < args.retries:
            attempt += 1
            text, last_error = "", None
            at_landing = not ops.landed
            if not ops.landed:
                state, last_error = land(ops, args)
                if last_error:
                    exit_failed = ops.proxy_failure(last_error) or None
                    if exit_failed:
                        break  # a different exit is the only thing that helps
                    state = "load_failed"
                elif not ops.landed:
                    text = ops.document_text()
            if ops.landed:
                at_landing = False
                status, text, waf, last_error = ops.fetch(req)
                state = ("load_failed" if last_error
                         else classify_fn(text, status, req.url, waf))
                if counts_as_blocked(state):
                    # A refusal of the session: the next attempt lands again.
                    ops.landed = False
            if state == "throttled" and throttles < THROTTLE_RETRIES:
                throttles += 1
                attempt -= 1  # a throttle wait spends its own budget (§24)
                pause = THROTTLE_WAIT_S * throttles
                log.warning("Rate-limited on %s (HTTP 429) — waiting %.0fs at "
                            "the same exit (%d/%d).", req.label, pause,
                            throttles, THROTTLE_RETRIES)
                ops.wait_ms(int(pause * 1000))
                continue
            if state in ("load_failed", "unknown") and attempt < args.retries:
                pause = args.retry_delay * (2 ** (attempt - 1))
                log.warning("%s came back %s (attempt %d/%d)%s — retrying in "
                            "%.1fs.", req.label, state, attempt, args.retries,
                            f": {mask(last_error)}" if last_error else "", pause)
                ops.wait_ms(int(pause * 1000))
                continue
            break

        if exit_failed and has_pool and block_attempt < block_retries:
            log.warning("Exit %s is unusable (%s) — rotating to another one "
                        "(%d/%d).", mask(pool.current), exit_failed,
                        block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            ops.relaunch()
            continue
        if (counts_as_blocked(state) and should_retry(state)
                and block_attempt < block_retries):
            _rotate(ops, pool, has_pool, "%s refused (%s)" % (req.label,
                    refusal_name(state)), block_attempt, block_retries, mask)
            continue
        break

    outcome.state = state
    if state == "load_failed" or exit_failed:
        outcome.load_failed = True
        log.error("Gave up on %s: %s", req.label,
                  mask(last_error or "the request never completed"))
    elif state == "rejected":
        outcome.rejected = api_error(text) or "HTTP 400"
        log.error("The endpoint refused this request (%s). That is a statement "
                  "about the PARAMETERS, and the same request sent again gets "
                  "the same answer, so it is not retried. If the query looks "
                  "right, the site's API has changed — open an issue with "
                  "--dump-html.", outcome.rejected)
        _dump(args, page_num, text)
    elif counts_as_blocked(state):
        outcome.blocked_by = refusal_name(state)
        debug = _save_debug(args, page_num, text)
        where = ("the homepage landing (%s), before %s was sent" % (ORIGIN_URL, req.label)
                 if at_landing else req.label)
        log.error("Blocked by %s on %s — saved to %s. This is exit 3, "
                  "distinct from an empty listing (exit 4). %s",
                  outcome.blocked_by, where, debug, refusal_advice(state))
    return outcome, text


def fetch_listing_page(ops, args, pool, query: Query, page_num: int,
                       mask: Callable[[str], str] = lambda s: s) -> PageOutcome:
    """Fetch and parse one page of the listing."""
    outcome, text = fetch_one_page(ops, args, pool, query, page_num, mask)
    if not outcome.ok:
        return outcome
    if not should_parse(outcome.state):
        # throttled past its budget, or never the endpoint's answer at all
        outcome.load_failed = True
        debug = _save_debug(args, page_num, text)
        log.error("Page %d never came back as the endpoint's answer (%s) — "
                  "saved to %s. %s", page_num, outcome.state, debug,
                  "Raise --delay, or spread the run over --proxy-file."
                  if outcome.state == "throttled" else "")
        return outcome

    _dump(args, page_num, text)
    rows = parse_page(text, query, page_num)
    outcome.products = rows
    outcome.total_available = total_results(text)
    outcome.pages_available = pages_available(outcome.total_available, query.page_size)
    log.info("Parsed %d row(s) from page %d.", len(rows), page_num)
    if page_num == 1 and outcome.total_available is not None:
        log.info("The site reports %d match(es) — %s page(s) of %d.",
                 outcome.total_available, outcome.pages_available, query.page_size)
    _core_field_warnings(rows, page_num)
    return outcome


# ---------------------------------------------------------------------------
# Resolving the query: the ZIP's stores, the category's id
# ---------------------------------------------------------------------------

def _json_state(text, status, url, waf=None) -> str:
    """The state of the store lookup's answer: JSON is content."""
    t = (text or "").lstrip()
    if t.startswith("{"):
        return "content"
    state = detect_document_state(text, status, url)
    return state if state in ("blocked", "challenge") else "unknown"


def _page_state(text, status, url, waf=None) -> str:
    """The state of a fetched category PAGE."""
    return detect_document_state(text, status, url)


class ResolveError(Exception):
    """The query cannot be sent: a usage error (exit 2) with the reason."""


def _served_or_failed(outcome: PageOutcome, what: str) -> bool:
    """True when a resolution request came back as the site's own answer.

    Anything else that survived the retries ("unknown": not the JSON, not
    the page, not a refusal) is marked as a load failure rather than parsed.
    Parsed, it would read as an ANSWER — an empty store list is "no store
    near this ZIP", a page with no id is "not a category" — and the run
    would stop with exit 2 and a reason that is false. Found by a planted
    control, not by a live run.
    """
    if outcome.ok and outcome.state != "content":
        outcome.load_failed = True
        log.error("%s never came back as the site's answer (%s) — nothing can "
                  "be concluded from it.", what, outcome.state)
    return outcome.ok


def resolve_query(ops, args, pool, query: Query,
                  mask: Callable[[str], str] = lambda s: s) -> Optional[PageOutcome]:
    """Fill in what the URL does not say: the stores, and the category id.

    Returns None when the query is ready, or a failed PageOutcome (page 1)
    when a request could not be completed — which the run reports exactly
    as if page 1 had failed, so a refusal here is exit 3 and a timeout
    exit 5. Raises ResolveError for an answer the user has to change: a ZIP
    with no store, a category that does not exist.
    """
    outcome, text = fetch_one_page(ops, args, pool, query, 1, mask,
                                   req=store_request(query.zip_code),
                                   classify_fn=_json_state)
    if not _served_or_failed(outcome, "The store lookup"):
        return outcome
    stores = parse_store_set(text, query.zip_code)
    if stores is None:
        raise ResolveError(
            "tractorsupply.com lists no store near ZIP %s, and its search "
            "endpoint refuses a request that names no store. Pass a --zip in "
            "the lower 48 states (the site answered 00000 and Anchorage's "
            "99501 with an empty store list)." % query.zip_code)
    query.stores = stores
    log.info("Pricing against store %s (%s) and %d nearby, zone %s, from ZIP %s.",
             stores.home, stores.home_store_name or "?", len(stores.store_ids) - 1,
             stores.zone, stores.zip_code)

    if query.mode != "category":
        return None
    outcome, text = fetch_one_page(ops, args, pool, query, 1, mask,
                                   req=category_page_request(query),
                                   classify_fn=_page_state)
    if outcome.state == "missing":
        raise ResolveError("/tsc/catalog/%s answered with the site's own error "
                           "page: there is no such category. The slug is the "
                           "last part of the category's address on the site."
                           % query.slug)
    if not _served_or_failed(outcome, "The category page"):
        return outcome
    details = category_details(text)
    if not details:
        raise ResolveError(
            "/tsc/catalog/%s was served but names no id the search endpoint "
            "filters on, in either place a category or a department page keeps "
            "one. The page layout has probably changed — open an issue."
            % query.slug)
    query.category_id = details["id"]
    query.category_name = details["name"]
    query.category_path = details["path"]
    if details.get("slug") and details["slug"] != query.slug:
        log.info("/tsc/catalog/%s resolved to %s — the site redirects that "
                 "address.", query.slug, details["slug"])
    log.info("%s %s: id %s (%s).", details.get("kind", "category").capitalize(),
             query.slug, query.category_id,
             query.category_path or query.category_name or "?")
    return None


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

# Below this share of page-1 rows mentioning the query, a search run warns
# that the site may have answered with something else (relevance_share).
RELEVANCE_FLOOR = 0.2


def run_pages(open_ops, close_ops, run_concurrently, args, pool,
              query: Query, concurrency: int,
              mask: Callable[[str], str] = lambda s: s) -> int:
    """The whole run after argument handling, shared by the three engines.

    `open_ops()` returns a ready ops object on `pool`, `close_ops(ops)`
    tears it down, and `run_concurrently(page_nums)` returns
    (outcomes, unattempted, exhausted) for pages 2..N fetched by workers.
    The engines supply those three because a browser's lifecycle (and on
    Playwright, its thread) is the one thing that cannot be shared.
    """
    outcomes: List[PageOutcome] = []
    blocked, stop_reason = False, "completed"
    extra = {}
    ops = open_ops()
    try:
        try:
            failed = resolve_query(ops, args, pool, query, mask)
        except ResolveError as e:
            log.error("%s", e)
            return 2
        if failed is not None:
            outcomes.append(failed)
            return finish(args, query, outcomes, stop_reason_for(failed),
                          failed.blocked_by is not None)

        # Page 1 is always fetched alone: its total decides how many pages
        # there are to address (§7).
        first = fetch_listing_page(ops, args, pool, query, 1, mask)
        outcomes.append(first)
        if query.mode == "search" and first.products:
            share = relevance_share(first.products, query.keyword)
            extra["query_relevance"] = None if share is None else round(share, 2)
            if share is not None and share < RELEVANCE_FLOOR:
                log.warning("Only %.0f%% of page 1 mentions %r in its title or "
                            "brand. The site answers a query it cannot match "
                            "with unrelated products rather than with nothing, "
                            "so these rows may not be what was searched for.",
                            100 * share, query.keyword)
        if not first.ok:
            stop_reason = stop_reason_for(first)
            blocked = first.blocked_by is not None
        else:
            plan = pages_to_plan(args.pages, first.pages_available)
            if plan < args.pages:
                log.info("Asked for %d page(s); the listing has %s. Fetching "
                         "all of them.", args.pages, first.pages_available)
            rest = list(range(2, plan + 1)) if first.products else []
            if rest and concurrency > 1:
                close_ops(ops)
                ops = None
                log.info("Fetching pages 2-%d across %d workers%s.", plan,
                         concurrency, f" over {len(pool)} exit(s)" if pool else "")
                more, unattempted, exhausted = run_concurrently(rest)
                outcomes.extend(more)
                failed_pages = [o for o in more if not o.ok]
                if failed_pages:
                    stop_reason = stop_reason_for(min(failed_pages, key=lambda o: o.page_num))
                    blocked = any(o.blocked_by for o in more)
                elif exhausted:
                    stop_reason = "end_of_listing"
                elif unattempted:
                    stop_reason = "pages_unattempted"
            else:
                for page_num in rest:
                    ops.wait_ms(int(args.delay * 1000))
                    if pool and pool.rotates_per_page():
                        pool.advance(f"per-page rotation, page {page_num}")
                        ops.relaunch()
                    outcome = fetch_listing_page(ops, args, pool, query, page_num, mask)
                    outcomes.append(outcome)
                    if not outcome.ok:
                        stop_reason = stop_reason_for(outcome)
                        blocked = outcome.blocked_by is not None
                        break
                    if not outcome.products:
                        # The listing shrank below the plan made from page 1.
                        # A property of the DATA, and complete.
                        log.info("Page %d came back empty — the listing ended "
                                 "before the plan did.", page_num)
                        stop_reason = "end_of_listing"
                        break
    finally:
        if ops is not None:
            close_ops(ops)
    return finish(args, query, outcomes, stop_reason, blocked, extra)


def worker_loop(ops, args, query: Query, work, results, results_lock,
                exhausted, name: str, mask: Callable[[str], str] = lambda s: s):
    """One concurrent worker's page loop, after its engine opened `ops`.

    Takes pages until the queue is empty or a page comes back empty (the
    listing ended before the plan did), which sets `exhausted` so the other
    workers stop taking work too. The query arrives already resolved.
    """
    first = True
    while not exhausted.is_set():
        try:
            page_num = work.get_nowait()
        except Exception:  # queue.Empty
            break
        if not first:
            ops.wait_ms(int(args.delay * 1000))
        first = False
        outcome = fetch_listing_page(ops, args, ops.pool, query, page_num, mask)
        with results_lock:
            results.append(outcome)
        if outcome.ok and not outcome.products:
            log.info("[%s] page %d returned no rows — the listing ended; "
                     "stopping dispatch.", name, page_num)
            exhausted.set()


def concurrency_for(args, pool) -> int:
    """How many workers this run may use, with the warnings said once for
    all three engines."""
    concurrency = max(1, args.concurrency)
    if concurrency <= 1:
        return 1
    if concurrency_limit(args.cdp_endpoint) == 1:
        log.warning("--concurrency is ignored with --cdp-endpoint: the Scraping "
                    "Browser API allows one live connection per profile, and "
                    "several workers would collide on it (profile_locked). Use "
                    "several pids instead.")
        return 1
    if not pool:
        log.warning("--concurrency %d with no proxy pool: every worker leaves "
                    "from the SAME address, which is N times the request rate "
                    "from it. Pass --proxy-file to spread the load.",
                    concurrency)
    if concurrency > 8:
        log.warning("--concurrency %d means %d browsers at once (~150-300MB "
                    "each).", concurrency, concurrency)
    return concurrency


def worker_pool(pool, worker_index: int):
    """A private ProxyPool for one worker, starting at a different exit.

    Workers start on distinct exits and share no mutable state, so rotation
    needs no lock (§7).
    """
    if not pool:
        return None
    from proxy_pool import ProxyPool
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")


def cdp_connect_hint(error_text: str) -> str:
    """What a failed --cdp-endpoint connection means, from its status.

    Two answers that want opposite fixes: a 401 is expired credentials (a
    profile's last about a day), a 500 is a profile another run still holds.
    """
    if "401" in (error_text or ""):
        return ("HTTP 401: the endpoint's credentials were refused. A Scraping "
                "Browser profile's credentials last about a day, so an "
                "endpoint copied from an older .env has usually expired. "
                "Get a fresh one from your 2Captcha dashboard.")
    return ("A Scraping Browser profile allows ONE live connection at a time, "
            "so an HTTP 500 here usually means another run still holds this "
            "`pid`. Wait for it to finish, or use a different pid.")


# Connecting to a Scraping Browser profile right after the previous run let
# go of it answers HTTP 500 `profile_locked`: the service releases a profile
# 1.6-1.9 s after a clean disconnect (measured by binance-scraper, and met
# again here on 2026-09-28 between two back-to-back probes). Three attempts
# 3 s apart ride that out, and a profile genuinely held by another run still
# fails, after ~9 s, with the pid explanation.
CDP_CONNECT_ATTEMPTS = 3
CDP_LOCKED_WAIT_S = 3.0
# pyppeteer does not surface the 500 at all: its connect() waits on a future
# the rejected handshake never resolves, so only a timeout ends it.
CDP_CONNECT_TIMEOUT_S = 10


def cdp_should_retry(error_text: str) -> bool:
    """Whether a failed --cdp-endpoint connection is worth another attempt:
    a locked profile (500) or a connect that never answered. A 401 is not:
    expired credentials stay expired."""
    text = error_text or ""
    if "401" in text:
        return False
    return ("profile_locked" in text or " 500" in text or "HTTP 500" in text
            or "did not return within" in text)
