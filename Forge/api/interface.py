from fastapi import APIRouter
import time

router = APIRouter(prefix="/forge", tags=["Forge"])

@router.get("/status")
async def get_status():
    return {
        "agent": "forge",
        "status": "active",
        "metrics": {
            "epoch": 42,
            "loss": 0.0021,
            "gpu_load": "92.4%"
        },
        "timestamp": time.time()
    }
