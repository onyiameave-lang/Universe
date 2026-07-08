from fastapi import APIRouter
import time

router = APIRouter(prefix="/atlas", tags=["Atlas"])

@router.get("/status")
async def get_status():
    return {
        "agent": "atlas",
        "status": "active",
        "metrics": {
            "corroboration_rate": "92%",
            "active_investigations": 4,
            "sources_indexed": 1240
        },
        "timestamp": time.time()
    }
