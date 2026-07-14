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
from pathlib import Path

_DATA_PATH = Path(__file__).resolve().parent / "data" / "vic_suburbs.json"
_cache: list[dict] | None = None


def list_all_suburbs() -> list[dict]:
    """All VIC localities as {"name": ..., "postcode": ...}, cached after
    the first call for the lifetime of the process."""
    global _cache
    if _cache is None:
        _cache = json.loads(_DATA_PATH.read_text())
    return _cache
