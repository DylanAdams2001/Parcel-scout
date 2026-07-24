"""realestate.com.au listing search.

There's no public API. The site embeds a JSON blob (window.ArgonautExchange)
in the server-rendered search-results page, which we extract with a real
browser. REA runs Kasada bot-detection (visible as a `window.KPSDK` block
page + HTTP 429), which a bare Playwright/Chromium session does not get
past. We use patchright (a Playwright fork with anti-detection patches),
headed (visible window, not headless) with a persistent browser profile
so cookies/session survive across runs - this matches how people actually
get through Kasada in practice. Even so, expect this to be fragile: it may
still get blocked periodically, and could need a manual CAPTCHA solve in
the visible window occasionally, or updates if REA changes their page
structure or defenses. Keep request rates modest.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

from patchright.async_api import Error as PlaywrightError
from patchright.async_api import async_playwright

PROFILE_DIR = Path(__file__).resolve().parent.parent / ".browser-profile"


def _proxy_config() -> dict | None:
    """Residential proxy config from the environment, or None to launch
    without one (the local-dev default). A datacenter IP (e.g. a cloud
    host's own address) is a strong signal to Kasada's bot detection, on
    top of everything the browser fingerprint itself gives away."""
    server = os.environ.get("PROXY_SERVER")
    if not server:
        return None
    config: dict = {"server": server}
    username = os.environ.get("PROXY_USERNAME")
    password = os.environ.get("PROXY_PASSWORD")
    if username:
        config["username"] = username
    if password:
        config["password"] = password
    return config
# launch_persistent_context can only have one Chromium process open on a
# given profile dir at a time - a second concurrent search (e.g. two users
# at once) would otherwise crash with "Opening in existing browser session".
# Serializing here just queues the second search instead.
_browser_lock = asyncio.Lock()


def _slugify_suburb(suburb: str, state: str, postcode: str | None) -> str:
    parts = [suburb.strip().lower().replace(" ", "-"), state.strip().lower()]
    if postcode:
        parts.append(postcode.strip())
    return "-".join(parts)


def _range_token(token: str, lo: int | None, hi: int | None) -> str | None:
    """Reproduce realestate.com.au's own range-filter URL segment format
    (reverse-engineered by driving their filter UI and reading the
    resulting URL): a missing lower bound becomes 0, a missing upper
    bound is simply dropped rather than substituted."""
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None:
        return f"{token}-{lo}-{hi}"
    if lo is not None:
        return f"{token}-{lo}"
    return f"{token}-0-{hi}"


SALE_METHOD_MISC = {
    "private_treaty": "ex-auctions",  # excludes auctions -> private treaty only
    "auction": "ex-private-sales",  # excludes private sales -> auction only
}

# realestate.com.au's own "Property type" filter (reverse-engineered the
# same way as everything else here - driving their filter UI and reading
# the resulting URL). Only "land" is wired up for now (the actual ask:
# excluding established houses on projects that shouldn't be a knockdown),
# but this is where more of their checkbox options would go if needed
# (Townhouse, Acreage, Rural, etc. each get their own "property-{x}" slug).
PROPERTY_TYPE_SEGMENT = {
    "land": "property-land",
}


def build_search_url(
    suburb: str,
    state: str,
    postcode: str | None = None,
    price_min: int | None = None,
    price_max: int | None = None,
    land_min: int | None = None,
    land_max: int | None = None,
    sale_method: str | None = None,
    property_type: str | None = None,
    page: int = 1,
) -> str:
    location = _slugify_suburb(suburb, state, postcode)
    segments = []
    property_token = PROPERTY_TYPE_SEGMENT.get(property_type or "")
    if property_token:
        segments.append(property_token)
    land_token = _range_token("size", land_min, land_max)
    if land_token:
        segments.append(land_token)
    price_token = _range_token("between", price_min, price_max)
    if price_token:
        segments.append(price_token)
    segments.append(f"in-{quote(location)}")
    path = "-".join(segments)
    url = f"https://www.realestate.com.au/buy/{path}/list-{page}"

    query = []
    misc = SALE_METHOD_MISC.get(sale_method or "")
    if misc:
        query.append(f"misc={misc}")
    if property_token or land_token or price_token:
        query.append("source=refinement")
    if query:
        url += "?" + "&".join(query)
    return url


def _build_bounding_box_url(
    kind: str,  # "buy" or "sold"
    price_min: int | None,
    price_max: int | None,
    land_min: int | None,
    land_max: int | None,
    sale_method: str | None,
    property_type: str | None,
    bounding_box: tuple[float, float, float, float],  # (north, west, south, east)
    page: int,
) -> str:
    """A location-free search: instead of `in-{suburb}`, a boundingBox query
    param covers an arbitrary area in one query - reverse-engineered from
    realestate.com.au's own "surrounding suburbs"/map-pan search (list view,
    not map view - it returns the same embedded ArgonautExchange structure
    the rest of this module already parses, whereas the map view uses a
    completely different, undocumented data shape)."""
    segments = []
    property_token = PROPERTY_TYPE_SEGMENT.get(property_type or "")
    if property_token:
        segments.append(property_token)
    land_token = _range_token("size", land_min, land_max)
    if land_token:
        segments.append(land_token)
    price_token = _range_token("between", price_min, price_max)
    if price_token:
        segments.append(price_token)
    path = "-".join(segments)
    url = f"https://www.realestate.com.au/{kind}/{path}/list-{page}" if path else f"https://www.realestate.com.au/{kind}/list-{page}"

    north, west, south, east = bounding_box
    query = [f"boundingBox={north},{west},{south},{east}"]
    misc = SALE_METHOD_MISC.get(sale_method or "")
    if misc:
        query.append(f"misc={misc}")
    query.append("source=refinement")
    url += "?" + "&".join(query)
    return url


def build_area_search_url(
    price_min: int | None,
    price_max: int | None,
    land_min: int | None,
    land_max: int | None,
    sale_method: str | None,
    property_type: str | None,
    bounding_box: tuple[float, float, float, float],
    page: int = 1,
) -> str:
    return _build_bounding_box_url("buy", price_min, price_max, land_min, land_max, sale_method, property_type, bounding_box, page)


def build_area_sold_search_url(
    price_min: int | None,
    price_max: int | None,
    land_min: int | None,
    land_max: int | None,
    sale_method: str | None,
    property_type: str | None,
    bounding_box: tuple[float, float, float, float],
    page: int = 1,
) -> str:
    return _build_bounding_box_url("sold", price_min, price_max, land_min, land_max, sale_method, property_type, bounding_box, page)


def _extract_argonaut(html: str) -> dict | None:
    match = re.search(r"window\.ArgonautExchange\s*=\s*(\{.+?\});\s*(?:</script>|\n)", html, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _extract_listings_from_argonaut(data: dict, search_key: str = "buySearch") -> list[dict]:
    """Pull real search-result listings out of the urqlClientCache.

    The page bundles the actual search results (e.g. buySearch.results.exact)
    alongside unrelated data (recently sold comparables, exclusive
    showcase/agency ads, suggested builder projects) under sibling keys of
    the same search object - so this targets `exact.items[].listing`
    specifically rather than scanning the whole tree, to avoid pulling in
    that noise. `exact` (not `surrounding`) matches the requested
    suburb/postcode exactly. `search_key` is "buySearch" for /buy/ pages or
    "soldSearch" for /sold/ pages - same shape, different root key.
    """
    listings = []
    seen_ids = set()

    widget = data.get("resi-property_listing-experience-web") or data.get(
        "resi-property_search-experience-web"
    )
    if not widget:
        return listings
    cache_raw = widget.get("urqlClientCache")
    if not isinstance(cache_raw, str):
        return listings
    try:
        cache = json.loads(cache_raw)
    except json.JSONDecodeError:
        return listings

    for entry in cache.values():
        entry_data = entry.get("data") if isinstance(entry, dict) else None
        if not isinstance(entry_data, str):
            continue
        try:
            parsed = json.loads(entry_data)
        except json.JSONDecodeError:
            continue
        search_root = parsed.get(search_key) if isinstance(parsed, dict) else None
        if not search_root:
            continue
        items = ((search_root.get("results") or {}).get("exact") or {}).get("items") or []
        for item in items:
            listing = item.get("listing")
            if not listing:
                continue
            lid = listing.get("id")
            if lid in seen_ids:
                continue
            seen_ids.add(lid)
            listings.append(listing)

    return listings


def _parse_land_size(land: dict) -> int | None:
    """displayValue is a comma-formatted string for sizes >= 1000
    (e.g. "2,212") - strip that so downstream float()/int() calls don't
    silently fail on larger blocks."""
    raw_value = (land or {}).get("displayValue")
    if not raw_value:
        return None
    try:
        return int(float(str(raw_value).replace(",", "")))
    except ValueError:
        return None


def _normalise_listing(raw: dict) -> dict:
    address = raw.get("address", {}) or {}
    display = address.get("display") or {}
    full_address = display.get("fullAddress") or address.get("fullAddress")

    land = (raw.get("propertySizes") or {}).get("land")
    land_size = _parse_land_size(land)
    land_unit = ((land or {}).get("sizeUnit") or {}).get("displayValue")

    price_obj = raw.get("price") or {}
    price = price_obj.get("display")

    return {
        "id": raw.get("id"),
        "address": full_address,
        "price_display": price,
        "land_size": land_size,
        "land_unit": land_unit,
        "property_type": (raw.get("propertyType") or {}).get("id"),
        "url": ((raw.get("_links") or {}).get("canonical") or {}).get("href"),
        "_raw": raw,
    }


def _normalise_sold_listing(raw: dict) -> dict:
    address = raw.get("address", {}) or {}
    display = address.get("display") or {}
    full_address = display.get("fullAddress") or address.get("fullAddress")

    land_size = _parse_land_size((raw.get("propertySizes") or {}).get("land"))

    return {
        "id": raw.get("id"),
        "address": full_address,
        "price_display": (raw.get("price") or {}).get("display"),
        "land_size": land_size,
        "property_type": (raw.get("propertyType") or {}).get("id"),
        "date_sold": (raw.get("dateSold") or {}).get("display"),
    }


def build_sold_search_url(
    suburb: str,
    state: str,
    postcode: str | None = None,
    price_min: int | None = None,
    price_max: int | None = None,
    land_min: int | None = None,
    land_max: int | None = None,
    sale_method: str | None = None,
    property_type: str | None = None,
    page: int = 1,
) -> str:
    """Same filters as build_search_url, applied to the /sold/ comps
    search too - an unfiltered "whole suburb" median mixes property types
    and price tiers that aren't representative of what's actually being
    searched (e.g. a $2M mansion's $/sqm skewing the median for a listing
    in a $500-700k search). property_type especially matters here - land
    sold comps must be compared against other land sales, not houses."""
    location = _slugify_suburb(suburb, state, postcode)
    segments = []
    property_token = PROPERTY_TYPE_SEGMENT.get(property_type or "")
    if property_token:
        segments.append(property_token)
    land_token = _range_token("size", land_min, land_max)
    if land_token:
        segments.append(land_token)
    price_token = _range_token("between", price_min, price_max)
    if price_token:
        segments.append(price_token)
    segments.append(f"in-{quote(location)}")
    path = "-".join(segments)
    url = f"https://www.realestate.com.au/sold/{path}/list-{page}"

    query = []
    misc = SALE_METHOD_MISC.get(sale_method or "")
    if misc:
        query.append(f"misc={misc}")
    if property_token or land_token or price_token:
        query.append("source=refinement")
    if query:
        url += "?" + "&".join(query)
    return url


async def _scrape_pages(
    context, url_for_page, search_key: str, max_pages: int, normalise
) -> list[dict]:
    results = []
    page = context.pages[0] if context.pages else await context.new_page()
    for page_num in range(1, max_pages + 1):
        url = url_for_page(page_num)
        print(f"[scraper] page {page_num}: {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except PlaywrightError as e:
            # A slow/blocked page shouldn't discard results already
            # gathered from earlier pages - stop paginating instead.
            print(f"[scraper] page {page_num} goto failed: {e}")
            break
        await page.wait_for_timeout(2500)
        html = await page.content()
        if "KPSDK" in html or "window.kpsdk" in html.lower():
            # Bot-challenge page. Give a human a chance to solve it in
            # the visible window before giving up on this run.
            print(f"[scraper] page {page_num}: bot-challenge detected, waiting for manual solve...")
            await page.wait_for_timeout(15000)
            html = await page.content()
        data = _extract_argonaut(html)
        if not data:
            print(f"[scraper] page {page_num}: no ArgonautExchange blob found, dumping to last_debug.html")
            (PROFILE_DIR.parent / "last_debug.html").write_text(html)
            break
        raw_listings = _extract_listings_from_argonaut(data, search_key)
        print(f"[scraper] page {page_num}: found {len(raw_listings)} raw listing records")
        if not raw_listings:
            (PROFILE_DIR.parent / "last_debug.html").write_text(html)
            break
        results.extend(normalise(r) for r in raw_listings)
        await page.wait_for_timeout(2000)  # be polite between pages
    return results


async def search_listings(
    suburb: str,
    state: str,
    postcode: str | None = None,
    price_min: int | None = None,
    price_max: int | None = None,
    land_min: int | None = None,
    land_max: int | None = None,
    sale_method: str | None = None,
    property_type: str | None = None,
    max_pages: int = 3,
) -> list[dict]:
    PROFILE_DIR.mkdir(exist_ok=True)
    async with _browser_lock:
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                locale="en-AU",
                viewport={"width": 1366, "height": 900},
                proxy=_proxy_config(),
            )
            try:
                return await _scrape_pages(
                    context,
                    lambda page_num: build_search_url(
                        suburb, state, postcode, price_min, price_max, land_min, land_max,
                        sale_method, property_type, page_num,
                    ),
                    "buySearch",
                    max_pages,
                    _normalise_listing,
                )
            finally:
                await context.close()


async def search_area_listings(
    price_min: int | None,
    price_max: int | None,
    land_min: int | None,
    land_max: int | None,
    sale_method: str | None,
    property_type: str | None,
    bounding_box: tuple[float, float, float, float],
    max_pages: int = 10,
) -> list[dict]:
    """Same as search_listings, but for an arbitrary geographic area
    (a bounding box around a center point + radius) rather than one named
    suburb - one query covering however large an area, instead of looping
    over every suburb inside it one at a time. See build_area_search_url."""
    PROFILE_DIR.mkdir(exist_ok=True)
    async with _browser_lock:
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                locale="en-AU",
                viewport={"width": 1366, "height": 900},
                proxy=_proxy_config(),
            )
            try:
                return await _scrape_pages(
                    context,
                    lambda page_num: build_area_search_url(
                        price_min, price_max, land_min, land_max, sale_method, property_type, bounding_box, page_num,
                    ),
                    "buySearch",
                    max_pages,
                    _normalise_listing,
                )
            finally:
                await context.close()


async def search_area_sold_listings(
    price_min: int | None,
    price_max: int | None,
    land_min: int | None,
    land_max: int | None,
    sale_method: str | None,
    property_type: str | None,
    bounding_box: tuple[float, float, float, float],
    max_pages: int = 2,
) -> list[dict]:
    """Sold comps for an area search - same land/sale-method filtering
    rationale as search_sold_listings, not filtered by price (see there)."""
    PROFILE_DIR.mkdir(exist_ok=True)
    async with _browser_lock:
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                locale="en-AU",
                viewport={"width": 1366, "height": 900},
                proxy=_proxy_config(),
            )
            try:
                return await _scrape_pages(
                    context,
                    lambda page_num: build_area_sold_search_url(
                        None, None, land_min, land_max, sale_method, property_type, bounding_box, page_num,
                    ),
                    "soldSearch",
                    max_pages,
                    _normalise_sold_listing,
                )
            finally:
                await context.close()


async def search_sold_listings(
    suburb: str,
    state: str,
    postcode: str | None = None,
    price_min: int | None = None,
    price_max: int | None = None,
    land_min: int | None = None,
    land_max: int | None = None,
    sale_method: str | None = None,
    property_type: str | None = None,
    max_pages: int = 2,
) -> list[dict]:
    """Recently-sold comparables for the suburb, used as market-price data
    for the value-score feature (see valuescore.py). Filtered the same way
    as the buy search, so the comps are actually representative of what's
    being searched rather than the whole suburb's market."""
    PROFILE_DIR.mkdir(exist_ok=True)
    async with _browser_lock:
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                locale="en-AU",
                viewport={"width": 1366, "height": 900},
                proxy=_proxy_config(),
            )
            try:
                return await _scrape_pages(
                    context,
                    lambda page_num: build_sold_search_url(
                        suburb, state, postcode, price_min, price_max, land_min, land_max,
                        sale_method, property_type, page_num,
                    ),
                    "soldSearch",
                    max_pages,
                    _normalise_sold_listing,
                )
            finally:
                await context.close()
