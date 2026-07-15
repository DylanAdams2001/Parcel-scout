"""One-suburb search+score pipeline: listings, sold comps, overlays/zoning,
location signals, and value scoring, combined into a single result set.

Shared by the single-suburb endpoint (main.py) and the multi-suburb batch
job runner (batch.py) - both just build a SearchRequest and call run_search.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import httpx
from pydantic import BaseModel

from location_signals import lookup_location_signals
from overlays import lookup_overlays_for_address
from scraper import search_listings, search_sold_listings
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


def _passes_filters(listing: dict, req: SearchRequest) -> bool:
    if req.land_min is not None or req.land_max is not None:
        size = listing.get("land_size")
        if size is None:
            return False
        try:
            size = float(size)
        except (TypeError, ValueError):
            return False
        if req.land_min is not None and size < req.land_min:
            return False
        if req.land_max is not None and size > req.land_max:
            return False
    return True


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
    listings = [l for l in listings if _passes_filters(l, req)]

    sold_comps = []
    if listings:
        # Same price/land/sale-method filters as the buy search, so the
        # comps are representative of what's actually being searched
        # rather than the whole suburb's market (a $2M mansion shouldn't
        # skew the median for a $500-700k search). Fixed at 1 page
        # regardless of the buy-side "pages per suburb" setting - sold
        # comps only feed the price-vs-median calculation, not something a
        # user browses, and one page (~20-25 comps) is normally plenty for
        # a stable median. Kept small deliberately: this runs once per
        # suburb, so it's a real time cost in batch mode.
        sold_comps = await search_sold_listings(
            suburb=req.suburb,
            state=req.state,
            postcode=req.postcode,
            price_min=req.price_min,
            price_max=req.price_max,
            land_min=req.land_min,
            land_max=req.land_max,
            sale_method=req.sale_method,
            max_pages=1,
        )

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

    if req.location_type or req.max_station_distance_km is not None:
        def _passes_location(l: dict) -> bool:
            loc = l.get("location")
            if loc is None:
                return False
            if req.location_type and loc["location_type"] != req.location_type:
                return False
            if req.max_station_distance_km is not None:
                station = loc.get("nearest_station")
                if not station or station["distance_km"] > req.max_station_distance_km:
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
