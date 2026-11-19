from __future__ import annotations

from typing import Any, Dict, Optional
from fastapi import APIRouter, Body, Query

from Nexus.api.coordination_bridge import (  # type: ignore
    coordinate_query,
    get_collective_memory_matrix,
    get_live_nexus_status,
    get_recent_dag_traces,
)

router = APIRouter(prefix="/nexus", tags=["Nexus"])


@router.get("/status")
async def get_status() -> Dict[str, Any]:
    """Return live Nexus DAG orchestration metrics, benchmark results, collaboration graph stats, and Scout ledger integrity."""
    return get_live_nexus_status()


@router.get("/traces")
async def get_traces(limit: int = Query(12, ge=1, le=50)) -> Dict[str, Any]:
    """Return recent topological DAG execution traces with provenance deduplication and Aegis policy gate verdicts."""
    return {
        "status": "ok",
        "traces": get_recent_dag_traces(limit=limit),
    }


@router.get("/memory-matrix")
async def get_memory_matrix(limit: int = Query(12, ge=1, le=50)) -> Dict[str, Any]:
    """Return the Two-Tier Chronicle Collective Memory Matrix (Tier-2 Promoted Reusable Knowledge vs. Tier-1 Episodic Traces)."""
    return {
        "status": "ok",
        **get_collective_memory_matrix(limit=limit),
    }


@router.post("/coordinate")
async def post_coordinate(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Run an institutional multi-agent DAG coordination session through Nexus."""
    query = str(payload.get("query") or payload.get("message") or "Coordinate ecosystem status")
    user_id = str(payload.get("user_id") or "operator")
    target_agent = str(payload.get("target_agent") or "nexus")
    trace = coordinate_query(query=query, user_id=user_id, target_agent=target_agent)
    return {
        "status": "complete",
        "agent": "nexus",
        "reply": trace["human_synthesis"],
        "response": trace["human_synthesis"],
        "trace": trace,
    }
