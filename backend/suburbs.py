"""Victorian suburb/locality name + postcode list, for search-box autocomplete.

Backs the suburb field's dropdown so a typo like "werribe" surfaces the
real locality ("Werribee") to click instead of silently searching for a
suburb that doesn't exist.

Two live Vicmap sources were tried first and both fell short:
  - The VMLITE_LOCALITY point layer (used by overlays.py/location_signals.py
    for other lookups) is a *cartographic label* dataset, not a complete
    locality list - it's missing real suburbs like Ferny Creek entirely.
  - The full LOCALITY_POLYGON boundary layer is complete but has no
    postcode field, and reverse-geocoding ~3,000+ centroids one at a time
    to recover postcodes was too slow/rate-limited to be workable.

Suburb-to-postcode is stable reference data (unlike overlays/zoning, which
must be live), so it's bundled locally instead: data/vic_suburbs.json,
derived from the open matthewproctor/australianpostcodes dataset, filtered
to VIC and deduped to one postcode per locality.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

_DATA_PATH = Path(__file__).resolve().parent / "data" / "vic_suburbs.json"
_cache: list[dict] | None = None


def list_all_suburbs() -> list[dict]:
    """All VIC localities as {"name", "postcode", "lat", "lon"}, cached
    after the first call for the lifetime of the process."""
    global _cache
    if _cache is None:
        _cache = json.loads(_DATA_PATH.read_text())
    return _cache


def find_suburb(name: str) -> dict | None:
    """Case-insensitive exact-name lookup, for resolving a batch search's
    center point to coordinates."""
    target = name.strip().lower()
    for s in list_all_suburbs():
        if s["name"].lower() == target:
            return s
    return None


_KM_PER_DEGREE_LAT = 111.0


def bounding_box_for(center_name: str, radius_km: float) -> tuple[float, float, float, float]:
    """(north, west, south, east) lat/lon rectangle covering radius_km
    around center_name - for realestate.com.au's boundingBox search param
    (see scraper.py's build_area_search_url), which searches a whole area
    in one query instead of looping over every suburb inside it. A
    rectangle, not a true circle - same shape realestate.com.au's own
    map-radius search produces. Raises ValueError if center_name doesn't
    match a known locality."""
    center = find_suburb(center_name)
    if center is None:
        raise ValueError(f"Unknown suburb: {center_name!r}")
    lat_delta = radius_km / _KM_PER_DEGREE_LAT
    km_per_degree_lon = _KM_PER_DEGREE_LAT * math.cos(math.radians(center["lat"]))
    lon_delta = radius_km / km_per_degree_lon
    north = center["lat"] + lat_delta
    south = center["lat"] - lat_delta
    west = center["lon"] - lon_delta
    east = center["lon"] + lon_delta
    return (north, west, south, east)
