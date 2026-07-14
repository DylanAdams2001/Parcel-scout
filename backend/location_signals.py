"""Infill/greenfield classification and train-station proximity.

Both come from the same public VicPlan ArcGIS services used elsewhere in
this tool (see overlays.py) - no auth, no key.
"""
from __future__ import annotations

import math
import re
from datetime import datetime

import httpx

GROWTH_AREAS_IDENTIFY_URL = (
    "https://plan-gis.mapshare.vic.gov.au/arcgis/rest/services/"
    "Planning/VicPlan_GrowthAreas/MapServer/identify"
)
ZONES_IDENTIFY_URL = (
    "https://plan-gis.mapshare.vic.gov.au/arcgis/rest/services/"
    "Planning/Vicplan_PlanningSchemeZones/MapServer/identify"
)
TRANSPORT_STATIONS_QUERY_URL = (
    "https://plan-gis.mapshare.vic.gov.au/arcgis/rest/services/"
    "Planning/VicPlan_Transport/MapServer/7/query"  # "PPTN Rail Station - existing/future"
)
# PPTN above only covers Melbourne's Principal Public Transport Network (its
# own layer extent is a ~80x80km metro box) - it returns nothing for
# regional addresses (Ballarat, Geelong, Bendigo, ...) even when a station
# is right there. This statewide Vicmap layer is the fallback for anywhere
# outside that box.
STATEWIDE_RAIL_QUERY_URL = (
    "https://services-ap1.arcgis.com/P744lA0wf4LlBZ84/ArcGIS/rest/services/"
    "Vicmap_Transport/FeatureServer/2/query"  # "Rail Infrastructure - Vicmap Transport"
)

_GREENFIELD_LAYER = "Land added to UGB since 2005"
_STATION_SEARCH_RADIUS_M = 15000  # covers all but the most remote outer-suburb addresses
_GREENFIELD_ZONE_CODES = {"UGZ"}  # Urban Growth Zone - applied specifically to PSP growth-area land
_RECENT_SUBDIVISION_YEARS = 8  # a residential zone gazetted this recently correlates with a newer estate
# Unlike the PPTN layer, the statewide rail layer has no "currently operating
# vs disused" field (its closest candidate, physical_condition, doesn't line
# up reliably - e.g. it marks the still-operating Clyde station same as some
# long-closed ones), so a genuinely closed regional station can still show
# up as the "nearest station" for a fallback lookup. This name filter only
# catches the unambiguous non-passenger infrastructure (freight sidings,
# junctions, yards), not closed-but-real-looking station names - a known gap.
_NON_PASSENGER_NAME = re.compile(r"\b(SIDING|JUNCTION|YARD|DEPOT|LOOP|CROSSING|GOODS)\b", re.IGNORECASE)


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_RESIDENTIAL_ZONE_GROUPS = {"GRZ", "NRZ", "RGZ", "TZ", "UGZ"}


async def _growth_area_match(client: httpx.AsyncClient, lon: float, lat: float) -> bool:
    """Checks the official 'land added to UGB since 2005' boundary layer.
    Confirmed (via real testing) to have real gaps - e.g. a Werribee South
    estate subdivided in 2018-2019 does not appear in it - so this is used
    as one signal among several, not the sole answer."""
    resp = await client.get(
        GROWTH_AREAS_IDENTIFY_URL,
        params={
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "sr": 4326,
            "layers": "all",
            "tolerance": 1,
            "mapExtent": f"{lon-0.1},{lat-0.1},{lon+0.1},{lat+0.1}",
            "imageDisplay": "400,400,96",
            "f": "json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return any(r.get("layerName") == _GREENFIELD_LAYER for r in data.get("results", []))


async def _current_zone(client: httpx.AsyncClient, lon: float, lat: float) -> dict | None:
    resp = await client.get(
        ZONES_IDENTIFY_URL,
        params={
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "sr": 4326,
            "layers": "all",
            "tolerance": 1,
            "mapExtent": f"{lon-0.1},{lat-0.1},{lon+0.1},{lat+0.1}",
            "imageDisplay": "400,400,96",
            "f": "json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results") or []
    if not results:
        return None
    attrs = results[0].get("attributes", {})
    gaz_date = None
    raw_date = attrs.get("GAZ_BEGIN_DATE")
    if raw_date:
        try:
            gaz_date = datetime.strptime(raw_date, "%m/%d/%Y")
        except ValueError:
            pass
    return {
        "code": attrs.get("ZONE_CODE_GROUP"),
        "gazetted": gaz_date,
    }


async def is_greenfield(client: httpx.AsyncClient, lon: float, lat: float) -> bool:
    """Best-effort classification combining three signals, since no single
    dataset reliably captures "is this a new estate": (1) the official
    growth-boundary layer, (2) an Urban Growth Zone designation (applied
    specifically to Precinct Structure Plan growth-area land), (3) a
    residential zone gazetted recently, which correlates with a newer
    subdivision even after it's later reclassified to standard residential
    zoning post-development. None of these is individually definitive -
    treat the result as a strong hint, not a certainty, especially near
    the boundary of an established suburb."""
    if await _growth_area_match(client, lon, lat):
        return True
    zone = await _current_zone(client, lon, lat)
    if not zone or zone["code"] not in _RESIDENTIAL_ZONE_GROUPS:
        return False
    if zone["code"] in _GREENFIELD_ZONE_CODES:
        return True
    if zone["gazetted"] and (datetime.now() - zone["gazetted"]).days < _RECENT_SUBDIVISION_YEARS * 365:
        return True
    return False


async def _nearest_metro_station(client: httpx.AsyncClient, lon: float, lat: float) -> dict | None:
    """Nearest station on Melbourne's Principal Public Transport Network -
    accurate "currently operating" status via PPTN_TYPE, but the layer's
    extent is a ~80x80km metro box, so it's empty for anywhere regional."""
    resp = await client.get(
        TRANSPORT_STATIONS_QUERY_URL,
        params={
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "distance": _STATION_SEARCH_RADIUS_M,
            "units": "esriSRUnit_Meter",
            "outFields": "LOCATION_NAME,PPTN_TYPE",
            "outSR": 4326,
            "returnGeometry": "true",
            "f": "json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    best = None
    for feature in data.get("features", []):
        attrs = feature.get("attributes", {})
        if attrs.get("PPTN_TYPE") != "Existing Station":
            continue
        geom = feature.get("geometry") or {}
        if "x" not in geom or "y" not in geom:
            continue
        dist = _haversine_km(lon, lat, geom["x"], geom["y"])
        if best is None or dist < best["distance_km"]:
            best = {"name": attrs.get("LOCATION_NAME"), "distance_km": round(dist, 2)}
    return best


async def _nearest_regional_station(client: httpx.AsyncClient, lon: float, lat: float) -> dict | None:
    """Fallback for addresses outside the metro PPTN layer's extent (regional
    Victoria - Ballarat, Geelong, Bendigo, ...), using the statewide Vicmap
    rail layer. See _NON_PASSENGER_NAME for its known accuracy gap."""
    resp = await client.get(
        STATEWIDE_RAIL_QUERY_URL,
        params={
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "distance": _STATION_SEARCH_RADIUS_M,
            "units": "esriSRUnit_Meter",
            "outFields": "feature_type_code,name",
            "outSR": 4326,
            "returnGeometry": "true",
            "f": "json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    best = None
    for feature in data.get("features", []):
        attrs = feature.get("attributes", {})
        name = attrs.get("name")
        if attrs.get("feature_type_code") != "rail_station" or not name:
            continue
        if _NON_PASSENGER_NAME.search(name):
            continue
        geom = feature.get("geometry") or {}
        if "x" not in geom or "y" not in geom:
            continue
        dist = _haversine_km(lon, lat, geom["x"], geom["y"])
        if best is None or dist < best["distance_km"]:
            best = {"name": name, "distance_km": round(dist, 2)}
    return best


async def nearest_station(client: httpx.AsyncClient, lon: float, lat: float) -> dict | None:
    """Nearest train station and straight-line distance in km, or None if
    nothing found within the search radius. Tries the accurate metro layer
    first, falling back to the statewide layer for regional addresses."""
    station = await _nearest_metro_station(client, lon, lat)
    if station is not None:
        return station
    return await _nearest_regional_station(client, lon, lat)


async def lookup_location_signals(client: httpx.AsyncClient, lon: float, lat: float) -> dict:
    greenfield = await is_greenfield(client, lon, lat)
    station = await nearest_station(client, lon, lat)
    return {
        "location_type": "greenfield" if greenfield else "infill",
        "nearest_station": station,
    }
