from fastapi import APIRouter
import time

router = APIRouter(prefix="/sentinel", tags=["Sentinel"])

@router.get("/status")
async def get_status():
    return {
        "agent": "sentinel",
        "status": "active",
        "metrics": {
            "credibility_avg": "96%",
            "news_velocity": "14/hr",
            "active_clusters": 8
        },
        "timestamp": time.time()
    }
