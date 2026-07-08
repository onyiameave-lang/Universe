"""
shared.mission_manager
======================
Track missions, assign agents, expose status. (Book I Part IV Article IV.)
"""
from __future__ import annotations
import copy
import threading
from typing import Any, Dict, List, Optional


class MissionManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._missions: Dict[str, Dict[str, Any]] = {}
        self._assignments: Dict[str, List[Dict[str, Any]]] = {}

    def create_mission(self, mission_id: str, description: str,
                      objectives: Optional[List[str]] = None, domain: str = "general",
                      metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        mission = {"id": mission_id, "description": description,
                  "objectives": list(objectives or []), "domain": domain,
                  "metadata": dict(metadata or {}), "agent_count": 0}
        with self._lock:
            self._missions[mission_id] = mission
            self._assignments[mission_id] = []
        return copy.deepcopy(mission)

    def assign_agent(self, mission_id: str, agent_name: str,
                    metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            if mission_id not in self._missions:
                self.create_mission(mission_id, "", domain="general")
            a = {"agent": agent_name, "metadata": dict(metadata or {})}
            self._assignments[mission_id].append(a)
            self._missions[mission_id]["agent_count"] = len(self._assignments[mission_id])
        return copy.deepcopy(a)

    def list_agents_for_mission(self, mission_id: str) -> List[str]:
        with self._lock:
            return [i["agent"] for i in self._assignments.get(mission_id, [])]

    def get_mission(self, mission_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            m = self._missions.get(mission_id)
            return copy.deepcopy(m) if m else None

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return {"missions": {mid: {"id": m["id"], "domain": m["domain"],
                    "description": m["description"], "objectives": list(m.get("objectives", [])),
                    "agent_count": m.get("agent_count", 0)} for mid, m in self._missions.items()}}
