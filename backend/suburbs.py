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


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_suburb(name: str) -> dict | None:
    """Case-insensitive exact-name lookup, for resolving a batch search's
    center point to coordinates."""
    target = name.strip().lower()
    for s in list_all_suburbs():
        if s["name"].lower() == target:
            return s
    return None


def suburbs_within_radius(center_name: str, radius_km: float) -> list[dict]:
    """All VIC localities within radius_km (straight-line) of center_name,
    nearest first, including the center itself. Raises ValueError if
    center_name doesn't match a known locality."""
    center = find_suburb(center_name)
    if center is None:
        raise ValueError(f"Unknown suburb: {center_name!r}")
    out = []
    for s in list_all_suburbs():
        dist = _haversine_km(center["lat"], center["lon"], s["lat"], s["lon"])
        if dist <= radius_km:
            out.append({**s, "distance_km": round(dist, 1)})
    out.sort(key=lambda s: s["distance_km"])
    return out
