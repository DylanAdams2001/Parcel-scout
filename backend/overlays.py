
"""Victorian planning overlay lookup.

Two public, no-auth ArcGIS REST services power VicPlan (mapshare.vic.gov.au):
  1. Geocoder: address -> lat/lon
  2. Vicplan_PlanningSchemeOverlays MapServer: point -> intersecting overlays

Both are unofficial-but-open (same services behind the public VicPlan map),
so keep request volume modest.
"""
from __future__ import annotations

import re

import httpx

_STATE_TOKENS = re.compile(r"\b(VIC|VICTORIA)\b", re.IGNORECASE)
_UNIT_WORD_PREFIX = re.compile(
    r"^(Level|Suite|Unit|Shop|Tenancy|Room|Entrance)\s+\S+,?\s*", re.IGNORECASE
)
_LETTERED_UNIT_SLASH_PREFIX = re.compile(r"^[A-Za-z]\d*/")


def _clean_address(address: str) -> str:
    """The Vicmap geocoder's SingleLine parser rejects addresses containing
    commas or a state name/abbreviation - strip both before querying."""
    cleaned = address.replace(",", " ")
    cleaned = _STATE_TOKENS.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _strip_unit_descriptors(address: str) -> str:
    """Fallback cleanup for addresses the geocoder can't parse as-is: it
    handles plain numeric unit/street-number pairs fine (e.g. "1104/401
    Docklands Drive") but chokes on worded unit descriptors ("Suite 1104/",
    "Level 27,", "Unit 212,", chained ones like "Entrance C4, Level 1,")
    and lettered unit prefixes ("G5/22 Synnot Street"). Strip those down to
    the plain street address, which is enough to resolve the parcel/overlay
    (overlays apply at the land-parcel level, not per-unit)."""
    cleaned = address
    while True:
        new = _UNIT_WORD_PREFIX.sub("", cleaned)
        if new == cleaned:
            break
        cleaned = new.strip()
    cleaned = _LETTERED_UNIT_SLASH_PREFIX.sub("", cleaned)
    return cleaned.strip()


GEOCODE_URL = (
    "https://corp-geo.mapshare.vic.gov.au/arcgis/rest/services/"
    "Geocoder/VMAddressEZIAdd/GeocodeServer/findAddressCandidates"
)
OVERLAY_IDENTIFY_URL = (
    "https://plan-gis.mapshare.vic.gov.au/arcgis/rest/services/"
    "Planning/Vicplan_PlanningSchemeOverlays/MapServer/identify"
)
ZONE_IDENTIFY_URL = (
    "https://plan-gis.mapshare.vic.gov.au/arcgis/rest/services/"
    "Planning/Vicplan_PlanningSchemeZones/MapServer/identify"
)


async def _try_geocode(client: httpx.AsyncClient, single_line: str) -> tuple[float, float] | None:
    resp = await client.get(
        GEOCODE_URL,
        params={"SingleLine": single_line, "f": "json", "maxLocations": 1},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        return None
    best = max(candidates, key=lambda c: c.get("score", 0))
    if best.get("score", 0) < 70:
        return None
    loc = best["location"]
    return loc["x"], loc["y"]


async def geocode_address(client: httpx.AsyncClient, address: str) -> tuple[float, float] | None:
    """Return (lon, lat) for a Victorian address, or None if no confident match."""
    cleaned = _clean_address(address)
    coords = await _try_geocode(client, cleaned)
    if coords is not None:
        return coords
    stripped = _strip_unit_descriptors(cleaned)
    if stripped != cleaned:
        return await _try_geocode(client, stripped)
    return None


async def get_overlays(client: httpx.AsyncClient, lon: float, lat: float) -> list[dict]:
    """Return overlay features intersecting a point, deduped by (code, description)."""
    resp = await client.get(
        OVERLAY_IDENTIFY_URL,
        params={
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "sr": 4326,
            "layers": "all",
            "tolerance": 1,
            "mapExtent": f"{lon-0.05},{lat-0.05},{lon+0.05},{lat+0.05}",
            "imageDisplay": "400,400,96",
            "f": "json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    seen = set()
    for feature in data.get("results", []):
        layer_name = feature.get("layerName", "")
        if layer_name.strip().lower() == "all overlays":
            continue  # aggregate layer duplicates the specific overlay layers below
        attrs = feature.get("attributes", {})
        code = attrs.get("ZONE_CODE", "")
        desc = attrs.get("ZONE_DESCRIPTION", "")
        group = attrs.get("ZONE_CODE_GROUP_LABEL", layer_name)
        key = (code, desc)
        if key in seen:
            continue
        seen.add(key)
        results.append({"layer": group, "code": code, "description": desc})
    return results


async def get_zones(client: httpx.AsyncClient, lon: float, lat: float) -> list[dict]:
    """Return zone features intersecting a point (usually just one - the
    property's base zone - but boundary/tolerance overlap can return more
    than one), deduped by (code, description)."""
    resp = await client.get(
        ZONE_IDENTIFY_URL,
        params={
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "sr": 4326,
            "layers": "all",
            "tolerance": 1,
            "mapExtent": f"{lon-0.05},{lat-0.05},{lon+0.05},{lat+0.05}",
            "imageDisplay": "400,400,96",
            "f": "json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    seen = set()
    for feature in data.get("results", []):
        layer_name = feature.get("layerName", "")
        if layer_name.strip().lower() == "all zones":
            continue  # aggregate layer duplicates the specific zone layer below
        attrs = feature.get("attributes", {})
        code = attrs.get("ZONE_CODE", "")
        desc = attrs.get("ZONE_DESCRIPTION", "")
        group = attrs.get("ZONE_CODE_GROUP_LABEL", layer_name)
        key = (code, desc)
        if key in seen:
            continue
        seen.add(key)
        results.append({"layer": group, "code": code, "description": desc})
    return results


async def lookup_overlays_for_address(client: httpx.AsyncClient, address: str) -> dict:
    """Geocode the address once and return both overlays and base zoning
    for that point - they come from separate VicPlan layers but share the
    same geocode, so it's cheaper to resolve them together."""
    coords = await geocode_address(client, address)
    if coords is None:
        return {"address": address, "matched": False, "overlays": [], "zones": []}
    lon, lat = coords
    overlays = await get_overlays(client, lon, lat)
    zones = await get_zones(client, lon, lat)
    return {
        "address": address,
        "matched": True,
        "lon": lon,
        "lat": lat,
        "overlays": overlays,
        "zones": zones,
    }
