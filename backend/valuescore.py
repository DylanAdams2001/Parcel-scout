"""Heuristic 0-100 "value score" for buy listings.

This is NOT a trained model - it's a transparent statistical heuristic
blending three signals, weighted by how the buyers-agent workflow this
tool serves actually prioritises them (location first, price second, land
size barely at all):

  - 60% location: infill (established area) vs greenfield, and distance to
    the nearest train station. Closer/infill scores higher.
  - 40% price: price-per-sqm-of-land vs the median of recent sold
    comparables in the suburb. Cheaper-than-median scores higher.
  - overlay risk is then subtracted as a penalty on top (see
    OVERLAY_PENALTY_WEIGHTS below) - a real cost/restriction to a buyer,
    not just an informational badge.

50 on either sub-component means "average/neutral", not "good" - a listing
priced exactly at the suburb median, or with unknown location data, isn't
being rewarded or punished, just left in the middle.

Known limitations, worth keeping in mind when reading the score:
- Land size is the only size signal used for the price component (no
  adjustment for dwelling size, bedrooms, condition, renovations) - two
  houses on identical land can have very different real value.
- The sold-comps sample is whatever realestate.com.au returns for the
  suburb (up to a couple of pages) - a small/unusual suburb may not have
  enough same-type comps for a stable median.
- Infill/greenfield classification is itself a best-effort heuristic (see
  location_signals.py) combining multiple government datasets, none of
  which is individually authoritative - treat it as a strong hint, not
  certainty, especially near an established suburb's edge.
- Listings with no numeric price (e.g. "Contact Agent", "Expressions of
  Interest") still get scored on location alone, since that's usually
  computable even when price isn't.
"""
from __future__ import annotations

import math
import re
import statistics

_NUMBER = re.compile(r"[\d,.]+")

LOCATION_WEIGHT = 0.6
PRICE_WEIGHT = 0.4
_STATION_DECAY_KM = 4  # station_score halves roughly every ~2.8km; see location_component()
_PRICE_LOG_SCALE = 120  # smaller = harder to reach a high price score; see price_component()
# Calibrated against a real example: a listing ~24% below the suburb's
# median $/sqm with a near-max location component (~96) previously scored
# 78 - too conservative for what's genuinely a strong buy. 120 moves that
# to ~86, and a deeper ~50% discount at the same location to ~92-94,
# leaving headroom above it for a location that's also literally perfect.
# This also makes the symmetric case - a listing priced ABOVE median -
# get punished harder for the same reason, which is intentional.
_UNKNOWN_PRICE_SCORE = 30  # below the neutral 50: an undisclosed price is a real drawback
# (can't confirm it's actually cheap) but shouldn't zero out an otherwise great listing.


def parse_price(display: str | None) -> float | None:
    """Extract a representative dollar figure from a price display string.

    Handles plain values ("$940,000"), ranges ("$649,000 - $699,000",
    "$735k - $765k", "$630,000-$660,000"), and price guides alongside other
    text ("AUCTION UNLESS SOLD PRIOR - $600,000-$660,000"). Displays with
    no digits at all ("Contact Agent", "Expressions of Interest", a bare
    "Auction") naturally yield no numbers and return None below.
    """
    if not display:
        return None

    numbers = []
    for match in _NUMBER.finditer(display):
        raw = match.group().rstrip(".").replace(",", "")
        if not raw or raw == ".":
            continue
        value = float(raw)
        # detect a trailing "k" right after this number, e.g. "735k"
        end = match.end()
        if end < len(display) and display[end].lower() == "k":
            value *= 1000
        numbers.append(value)

    if not numbers:
        return None
    # A lone number under 1000 (e.g. a stray "1" from "1104/401") isn't a
    # price - real AU property prices are always well above that.
    numbers = [n for n in numbers if n >= 1000]
    if not numbers:
        return None
    return sum(numbers) / len(numbers)


def price_per_sqm(price: float | None, land_size: str | float | None) -> float | None:
    if price is None or land_size is None:
        return None
    try:
        size = float(land_size)
    except (TypeError, ValueError):
        return None
    if size <= 0:
        return None
    return price / size


def median_price_per_sqm(sold_comps: list[dict], property_type: str | None) -> float | None:
    """Median $/sqm across sold comps, preferring same property type but
    falling back to all comps if there aren't enough (< 3) of that type."""
    def rates(comps):
        out = []
        for c in comps:
            rate = price_per_sqm(parse_price(c.get("price_display")), c.get("land_size"))
            if rate:
                out.append(rate)
        return out

    same_type = [c for c in sold_comps if property_type and c.get("property_type") == property_type]
    values = rates(same_type)
    if len(values) < 3:
        values = rates(sold_comps)
    if not values:
        return None
    return statistics.median(values)


def price_component(listing: dict, sold_comps: list[dict]) -> float | None:
    """0-100, centered on 50 = priced right at the suburb median $/sqm.
    Uses log(ratio) so a listing at half the median rate and one at double
    the median rate move the score by the same amount in opposite
    directions (a plain linear ratio isn't symmetric like that). Uses
    _PRICE_LOG_SCALE (deliberately less steep than a "naive" curve) so it
    takes a genuinely large discount to reach a high score, not just a
    mildly-cheaper-than-average one. None if there's no usable price or
    land size."""
    price = parse_price(listing.get("price_display"))
    rate = price_per_sqm(price, listing.get("land_size"))
    if rate is None:
        return None
    median_rate = median_price_per_sqm(sold_comps, listing.get("property_type"))
    if not median_rate:
        return None
    ratio = rate / median_rate
    score = 50 - math.log(ratio) * _PRICE_LOG_SCALE
    return max(0.0, min(100.0, score))


def location_component(location: dict | None) -> float:
    """0-100 blending infill status (50% of this component) and distance
    to the nearest train station (the other 50%), each independently
    0-100. Infill = 100, greenfield = 0. Station distance decays smoothly
    (100 at the station, ~50 at _STATION_DECAY_KM, approaching 0 further
    out) rather than a hard cutoff, so closer is always at least somewhat
    better. Unknown location data defaults to a neutral 50 rather than
    penalising a listing just because it didn't geocode."""
    if location is None:
        return 50.0
    infill_score = 100.0 if location.get("location_type") == "infill" else 0.0
    station = location.get("nearest_station")
    if station and station.get("distance_km") is not None:
        station_score = 100.0 * math.exp(-station["distance_km"] / _STATION_DECAY_KM)
    else:
        station_score = 50.0  # no station found within search radius - neutral, not punished
    return (infill_score + station_score) / 2


def score_listing(listing: dict, sold_comps: list[dict]) -> tuple[float, float]:
    """Returns (display_score, raw_score): display is clamped 0-100 for
    readability (before the overlay penalty below), raw is unclamped so
    results that tie at the display ceiling/floor can still be ranked
    meaningfully underneath.

    Combines location and price with a weighted GEOMETRIC mean rather than
    a plain weighted average. That's deliberate: an arithmetic mean lets a
    great score in one component partly paper over a mediocre one (a
    so-so location + a great price can still average out high). A
    geometric mean can't be propped up like that - if either component is
    weak, the whole score drops hard, so only a listing that's genuinely
    strong on BOTH price and location reaches the top of the range.

    A listing with no disclosed/parseable price ("Contact Agent",
    "Expressions of Interest") is NOT scored on location alone - that let
    well-located-but-unpriced listings score as if they were confirmed
    cheap, which they aren't (we simply don't know). Treated instead as
    below-average on price (see _UNKNOWN_PRICE_SCORE) - an unverifiable
    price is a real drawback, not a neutral unknown."""
    price = price_component(listing, sold_comps)
    if price is None:
        price = _UNKNOWN_PRICE_SCORE
    location = location_component(listing.get("location"))
    if location <= 0 or price <= 0:
        raw = 0.0
    else:
        raw = math.exp(LOCATION_WEIGHT * math.log(location) + PRICE_WEIGHT * math.log(price))
    return max(0.0, min(100.0, round(raw))), raw


# Points deducted per overlay family, roughly by how much it actually
# constrains a buyer (cost, delay, or loss of development/renovation
# rights) rather than by how common it is. Matched by stripping trailing
# digits/letters off the overlay code (e.g. "HO1235" -> "HO", "ESO1" ->
# "ESO") so specific schedule numbers all map to their family's weight.
OVERLAY_PENALTY_WEIGHTS = {
    # Severe: risk of losing the land, or a contamination finding that can
    # block use/finance entirely until resolved.
    "PAO": 25,  # Public Acquisition Overlay - land can be compulsorily acquired
    "EAO": 25,  # Environmental Audit Overlay - potential soil/site contamination
    # High: materially restricts what can be built, or adds large,
    # hard-to-avoid construction cost (bushfire-rated materials, flood
    # engineering, heritage approval process).
    "HO": 15,  # Heritage Overlay - restricts renovation/demolition
    "BMO": 15,  # Bushfire Management Overlay - costly construction requirements
    "WMO": 15,  # Wildfire Management Overlay (older bushfire overlay name)
    "LSIO": 15,  # Land Subject to Inundation Overlay - flood risk
    "FO": 15,  # Floodway Overlay - flood risk
    "SBO": 12,  # Special Building Overlay - flood/stormwater risk
    "MAEO": 15,  # Melbourne Airport Environs Overlay - noise-affected, restricts noise-sensitive uses
    "PSAEO": 15,  # Public Safety Airport Environs Overlay - noise + safety-affected, similar restrictions
    # Medium: real constraint on what can be done with the land, but
    # rarely a dealbreaker.
    "ESO": 8,  # Environmental Significance Overlay
    "VPO": 8,  # Vegetation Protection Overlay - restricts tree removal
    "SLO": 8,  # Significant Landscape Overlay
    "EMO": 10,  # Erosion Management Overlay
    # Low: administrative/design constraints or a cost (levy), not a
    # restriction on use.
    "DDO": 4,  # Design and Development Overlay
    "DCPO": 4,  # Development Contributions Plan Overlay - a fee, not a use restriction
    "DPO": 4,  # Development Plan Overlay
    "IPO": 4,  # Incorporated Plan Overlay
    "ICO": 4,  # Incorporated Plan Overlay (alt code)
}
_DEFAULT_OVERLAY_PENALTY = 2  # minor/administrative overlays not listed above (e.g. PO, SCO)
_MAX_TOTAL_OVERLAY_PENALTY = 40  # a pile of minor overlays shouldn't crush the score


def _overlay_family(code: str) -> str:
    return re.sub(r"[\d\-].*$", "", code or "").strip().upper()


def overlay_penalty(overlays: list[dict]) -> int:
    """Total point deduction for a listing's overlays, deduplicated by
    family so e.g. two Heritage Overlay schedules don't double-penalise."""
    families = {_overlay_family(o.get("code", "")) for o in overlays}
    families.discard("")
    total = sum(OVERLAY_PENALTY_WEIGHTS.get(f, _DEFAULT_OVERLAY_PENALTY) for f in families)
    return min(total, _MAX_TOTAL_OVERLAY_PENALTY)


def score_listings(listings: list[dict], sold_comps: list[dict]) -> None:
    """Mutates each listing dict in place, adding 'value_score' (0-100 for
    display) and 'value_score_raw' (unclamped, for sorting), folding in
    price, location, and overlay-risk penalty. Must run after both the
    overlay lookup ('overlay_result') and location lookup ('location')
    have already populated each listing."""
    for listing in listings:
        display, raw = score_listing(listing, sold_comps)
        overlays = (listing.get("overlay_result") or {}).get("overlays") or []
        penalty = overlay_penalty(overlays)
        raw -= penalty
        listing["value_score_raw"] = raw
        listing["value_score"] = max(0.0, min(100.0, round(raw)))
