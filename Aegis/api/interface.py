from fastapi import APIRouter
import time

router = APIRouter(prefix="/aegis", tags=["Aegis"])

@router.get("/status")
async def get_status():
    return {
        "agent": "aegis",
        "status": "active",
        "metrics": {
            "risk_index": 15,
            "compliance_score": 98.2,
            "threats_detected": 0
        },
        "timestamp": time.time()
    }
