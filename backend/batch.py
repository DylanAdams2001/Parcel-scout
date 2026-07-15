"""Multi-suburb ("batch") search: run the same filters across every VIC
suburb within a radius of a center point, sequentially, aggregating results
into one ranked list.

Deliberately sequential and slow (one suburb at a time, with a pause
between each) rather than parallel - the scraper already serializes
browser launches (see scraper.py's _browser_lock) since realestate.com.au's
bot detection weighs request-rate heavily, and a batch of dozens of
suburbs run back-to-back is already a much bigger burst of traffic than
this tool was originally used for. This is meant to run in the background
for tens of minutes to a few hours, not to be instant.

Jobs are tracked in memory only (no database) - fine for a single-user
local/self-hosted tool, but a server restart loses in-progress/completed
jobs.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from search import SearchRequest, run_search
from suburbs import suburbs_within_radius

router = APIRouter()

_SUBURB_DELAY_SECONDS = 5  # pause between suburbs, on top of scraper.py's own per-page delays
_ESTIMATED_SECONDS_PER_SUBURB = 90  # rough - varies a lot with listing count per suburb

_jobs: dict[str, dict] = {}


class BatchPreviewResponse(BaseModel):
    suburb_count: int
    suburbs: list[str]
    estimated_minutes: int


@router.get("/api/batch/preview", response_model=BatchPreviewResponse)
async def batch_preview(center: str, radius_km: float):
    try:
        matches = suburbs_within_radius(center, radius_km)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    total_seconds = len(matches) * (_ESTIMATED_SECONDS_PER_SUBURB + _SUBURB_DELAY_SECONDS)
    return BatchPreviewResponse(
        suburb_count=len(matches),
        suburbs=[s["name"] for s in matches],
        estimated_minutes=max(1, round(total_seconds / 60)),
    )


class BatchSearchRequest(BaseModel):
    center: str
    radius_km: float
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    land_min: Optional[int] = None
    land_max: Optional[int] = None
    sale_method: Optional[str] = None
    location_type: Optional[str] = None
    max_station_distance_km: Optional[float] = None
    max_pages: int = 2  # kept low by default - this multiplies by suburb count


class BatchStartResponse(BaseModel):
    job_id: str
    suburb_count: int


@router.post("/api/batch/start", response_model=BatchStartResponse)
async def batch_start(req: BatchSearchRequest):
    try:
        matches = suburbs_within_radius(req.center, req.radius_km)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not matches:
        raise HTTPException(status_code=400, detail="No suburbs found in that radius")

    job_id = uuid.uuid4().hex[:12]
    _jobs[job_id] = {
        "status": "running",
        "center": req.center,
        "radius_km": req.radius_km,
        "suburbs": [s["name"] for s in matches],
        "total": len(matches),
        "completed": 0,
        "current_suburb": None,
        "results": [],
        "suburb_errors": {},
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    }
    asyncio.create_task(_run_batch_job(job_id, matches, req))
    return BatchStartResponse(job_id=job_id, suburb_count=len(matches))


async def _run_batch_job(job_id: str, suburbs: list[dict], req: BatchSearchRequest) -> None:
    job = _jobs[job_id]
    for i, suburb in enumerate(suburbs):
        job["current_suburb"] = suburb["name"]
        search_req = SearchRequest(
            suburb=suburb["name"],
            state="vic",
            postcode=suburb.get("postcode"),
            price_min=req.price_min,
            price_max=req.price_max,
            land_min=req.land_min,
            land_max=req.land_max,
            sale_method=req.sale_method,
            location_type=req.location_type,
            max_station_distance_km=req.max_station_distance_km,
            max_pages=req.max_pages,
        )
        try:
            result = await run_search(search_req)
            for listing in result["results"]:
                listing["suburb"] = suburb["name"]
            job["results"].extend(result["results"])
            job["results"].sort(key=lambda l: -(l.get("value_score") or 0))
        except Exception as e:  # noqa: BLE001 - one suburb's failure shouldn't abort the batch
            job["suburb_errors"][suburb["name"]] = str(e)
        job["completed"] = i + 1
        if i < len(suburbs) - 1:
            await asyncio.sleep(_SUBURB_DELAY_SECONDS)
    job["status"] = "done"
    job["current_suburb"] = None
    job["finished_at"] = datetime.now(timezone.utc).isoformat()


@router.get("/api/batch/{job_id}")
async def batch_status(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    return job
