from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter

_GENESIS_ROOT = Path(__file__).resolve().parents[1]
if str(_GENESIS_ROOT) not in sys.path:
    sys.path.insert(0, str(_GENESIS_ROOT))

from core.control_plane import get_genesis_control_plane  # type: ignore

router = APIRouter(prefix="/genesis", tags=["Genesis"])


@router.get("/status")
async def get_status() -> Dict[str, Any]:
    cp = get_genesis_control_plane()
    snap = cp.get_dashboard_snapshot()
    return {
        "agent": "genesis",
        "status": "active",
        "architecture_version": snap.get("architecture_version"),
        "metrics": snap.get("metrics", {}),
        "overview": snap.get("overview"),
        "timestamp": time.time(),
    }


@router.get("/cases")
async def list_cases() -> Dict[str, Any]:
    cp = get_genesis_control_plane()
    return cp.get_dashboard_snapshot()


@router.post("/cases/open")
async def open_case(payload: Dict[str, Any]) -> Dict[str, Any]:
    cp = get_genesis_control_plane()
    return cp.open_case(
        title=str(payload.get("title") or ""),
        domain=str(payload.get("domain") or ""),
        trigger_type=str(payload.get("trigger_type") or "unmet_user_request"),
        problem_statement=str(payload.get("problem_statement") or ""),
        requested_by=str(payload.get("requested_by") or "operator"),
        parent_case_id=payload.get("parent_case_id"),
        preferred_decision=str(payload.get("preferred_decision") or "auto"),
        proposed_name=payload.get("proposed_name"),
        capabilities=payload.get("capabilities"),
        security_level=str(payload.get("security_level") or "standard"),
        worker_step=int(payload.get("worker_step", 2)),
        auto_advance_to_review=bool(payload.get("auto_advance_to_review", True)),
    )


@router.post("/cases/{case_id}/advance")
async def advance_case(case_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    body = payload or {}
    cp = get_genesis_control_plane()
    return cp.advance_case(
        case_id,
        target_stage=body.get("target_stage"),
        override_decision=body.get("override_decision"),
        worker_step=body.get("worker_step"),
    )


@router.post("/cases/{case_id}/approve")
async def approve_case(case_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    body = payload or {}
    cp = get_genesis_control_plane()
    return cp.approve_and_deploy_case(
        case_id,
        human_approver=str(body.get("human_approver") or "operator"),
        release_scope=str(body.get("release_scope") or "staged_canary"),
        human_confirm=bool(body.get("human_confirm", True)),
    )


@router.post("/cases/{case_id}/rollback")
async def rollback_case(case_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    body = payload or {}
    cp = get_genesis_control_plane()
    return cp.rollback_case(
        case_id,
        reason=str(body.get("reason") or "Operator triggered canary rollback"),
    )
