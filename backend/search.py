"""Search+score pipeline: listings, sold comps, overlays/zoning, location
signals, and value scoring, combined into a single result set.

Two entry points sharing the same enrich/score logic:
  - run_search: one named suburb (main.py's /api/search)
  - run_area_search: an arbitrary area via a lat/lon bounding box
    (main.py's /api/area-search) - one query covering however large an
    area, instead of looping over every suburb inside it one at a time.
    See scraper.py's build_area_search_url for how that's possible.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import httpx
from pydantic import BaseModel

from location_signals import lookup_location_signals
from overlays import lookup_overlays_for_address
from scraper import (
    search_area_listings,
    search_area_sold_listings,
    search_listings,
    search_sold_listings,
)
from suburbs import bounding_box_for
from valuescore import score_listings


class SearchRequest(BaseModel):
    suburb: str
    state: str = "vic"
    postcode: Optional[str] = None
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    land_min: Optional[int] = None
    land_max: Optional[int] = None
    sale_method: Optional[str] = None  # "private_treaty", "auction", or None for any
    location_type: Optional[str] = None  # "infill", "greenfield", or None for any
    max_station_distance_km: Optional[float] = None
    max_pages: int = 3


class AreaSearchRequest(BaseModel):
    center: str
    radius_km: float
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    land_min: Optional[int] = None
    land_max: Optional[int] = None
    sale_method: Optional[str] = None
    location_type: Optional[str] = None
    max_station_distance_km: Optional[float] = None
    max_pages: int = 10


def _passes_land_filter(listing: dict, land_min: int | None, land_max: int | None) -> bool:
    if land_min is not None or land_max is not None:
        size = listing.get("land_size")
        if size is None:
            return False
        try:
            size = float(size)
        except (TypeError, ValueError):
            return False
        if land_min is not None and size < land_min:
            return False
        if land_max is not None and size > land_max:
            return False
    return True


async def _enrich_and_score(
    listings: list[dict],
    sold_comps: list[dict],
    location_type: str | None,
    max_station_distance_km: float | None,
) -> dict:
    """Given raw listings + sold comps, adds overlays/zoning, location
    signals (infill/greenfield + nearest station), and a value score to
    each listing, applying the location-based filters and sorting by
    score. Shared by run_search and run_area_search."""
    async with httpx.AsyncClient() as client:
        overlay_results = await asyncio.gather(
            *[
                lookup_overlays_for_address(client, l["address"])
                for l in listings
                if l.get("address")
            ],
            return_exceptions=True,
        )

    overlays_by_address = {}
    for l, res in zip([l for l in listings if l.get("address")], overlay_results):
        if isinstance(res, Exception):
            overlays_by_address[l["address"]] = {"matched": False, "overlays": [], "zones": [], "error": str(res)}
        else:
            overlays_by_address[l["address"]] = res

    for l in listings:
        l["overlay_result"] = overlays_by_address.get(l.get("address"), {"matched": False, "overlays": [], "zones": []})

    # Location signals reuse the lon/lat already resolved during overlay
    # geocoding above, rather than geocoding each address a second time.
    async with httpx.AsyncClient() as client:
        geocoded = [l for l in listings if l["overlay_result"].get("matched")]
        location_results = await asyncio.gather(
            *[
                lookup_location_signals(client, l["overlay_result"]["lon"], l["overlay_result"]["lat"])
                for l in geocoded
            ],
            return_exceptions=True,
        )
    for l, res in zip(geocoded, location_results):
        l["location"] = None if isinstance(res, Exception) else res
    for l in listings:
        l.setdefault("location", None)

    if location_type or max_station_distance_km is not None:
        def _passes_location(l: dict) -> bool:
            loc = l.get("location")
            if loc is None:
                return False
            if location_type and loc["location_type"] != location_type:
                return False
            if max_station_distance_km is not None:
                station = loc.get("nearest_station")
                if not station or station["distance_km"] > max_station_distance_km:
                    return False
            return True

        listings = [l for l in listings if _passes_location(l)]

    if listings:
        # Must run after both the overlay and location lookups above -
        # the score blends price-vs-comps, location (infill/greenfield +
        # station distance), and an overlay-risk penalty into one number.
        score_listings(listings, sold_comps)
        listings.sort(key=lambda l: -l["value_score_raw"])

    for l in listings:
        l.pop("_raw", None)
        l.pop("value_score_raw", None)

    return {"count": len(listings), "results": listings}


async def run_search(req: SearchRequest) -> dict:
    """Runs one suburb's full search+score pipeline (listings, sold comps,
    overlays/zoning, location signals, scoring)."""
    listings = await search_listings(
        suburb=req.suburb,
        state=req.state,
        postcode=req.postcode,
        price_min=req.price_min,
        price_max=req.price_max,
        land_min=req.land_min,
        land_max=req.land_max,
        sale_method=req.sale_method,
        max_pages=req.max_pages,
    )
    listings = [l for l in listings if _passes_land_filter(l, req.land_min, req.land_max)]

    sold_comps = []
    if listings:
        # Land size + sale-method filters, like the buy search - both are
        # legitimate "comparable properties" filters. Price is deliberately
        # NOT filtered here: valuescore.py compares each listing's price
        # against these comps, so pre-filtering the comps to the search's
        # own price range would be circular (everything in a $500-700k
        # search would trivially look "average" against comps that were
        # only ever $500-700k to begin with). Fixed at 1 page regardless of
        # the buy-side "pages per suburb" setting - sold comps only feed
        # the price comparison, not something a user browses, and one page
        # (~20-25 comps) is normally plenty.
        sold_comps = await search_sold_listings(
            suburb=req.suburb,
            state=req.state,
            postcode=req.postcode,
            land_min=req.land_min,
            land_max=req.land_max,
            sale_method=req.sale_method,
            max_pages=1,
        )

    return await _enrich_and_score(listings, sold_comps, req.location_type, req.max_station_distance_km)


async def run_area_search(req: AreaSearchRequest) -> dict:
    """Runs the full search+score pipeline across an arbitrary area (a
    bounding box around req.center, req.radius_km wide) rather than one
    suburb - a single query instead of looping over every suburb inside
    the radius. Raises ValueError if req.center isn't a known locality."""
    bounding_box = bounding_box_for(req.center, req.radius_km)

    listings = await search_area_listings(
        price_min=req.price_min,
        price_max=req.price_max,
        land_min=req.land_min,
        land_max=req.land_max,
        sale_method=req.sale_method,
        bounding_box=bounding_box,
        max_pages=req.max_pages,
    )
    listings = [l for l in listings if _passes_land_filter(l, req.land_min, req.land_max)]

    sold_comps = []
    if listings:
        sold_comps = await search_area_sold_listings(
            price_min=None,
            price_max=None,
            land_min=req.land_min,
            land_max=req.land_max,
            sale_method=req.sale_method,
            bounding_box=bounding_box,
            max_pages=2,
        )

    return await _enrich_and_score(listings, sold_comps, req.location_type, req.max_station_distance_km)
