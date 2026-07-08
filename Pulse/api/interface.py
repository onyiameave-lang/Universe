from fastapi import APIRouter
import time

router = APIRouter(prefix="/pulse", tags=["Pulse"])

@router.get("/status")
async def get_status():
    return {
        "agent": "pulse",
        "status": "active",
        "metrics": {
            "sentiment_index": 72.4,
            "volatility": "MED",
            "trending_topics": 12
        },
        "timestamp": time.time()
    }
