from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from search import AreaSearchRequest, SearchRequest, run_area_search, run_search
from suburbs import list_all_suburbs

app = FastAPI(title="Realestate + Overlay Search")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/api/suburbs")
async def api_suburbs():
    return {"results": list_all_suburbs()}


@app.post("/api/search")
async def api_search(req: SearchRequest):
    return await run_search(req)


@app.post("/api/area-search")
async def api_area_search(req: AreaSearchRequest):
    try:
        return await run_area_search(req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
