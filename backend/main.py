from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from batch import router as batch_router
from search import SearchRequest, run_search
from suburbs import list_all_suburbs

app = FastAPI(title="Realestate + Overlay Search")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/api/suburbs")
async def api_suburbs():
    return {"results": list_all_suburbs()}


@app.post("/api/search")
async def api_search(req: SearchRequest):
    return await run_search(req)


app.include_router(batch_router)

app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
