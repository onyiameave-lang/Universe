"""
Genesis.core.agent_registry
==========================
Registry and version control for created agents. (Book I Part IV Article III
Identity is permanent; Article XIII Evolution; Article XIV Retirement; Book IV
Part II Ch XII Repository Evolution.)

Institutional creation demands a full lifecycle, not fire-and-forget:
  * REGISTER    every created agent gets a permanent identity + version record.
  * VERSION     re-creating/evolving an agent bumps its version and preserves
                the prior one (rollback is possible; nothing dies without record).
  * LINEAGE     tracks which blueprint and which prior version each agent came from.
  * RETIRE      an agent can be retired with its knowledge preserved.
  * ROLLBACK    restore a prior version's files if a new one misbehaves.

Persists to disk. This is the constitutional memory of everything Genesis builds.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


class CreatedAgentRegistry:
    def __init__(self, storage_dir: str = "registry"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.versions_dir = self.storage_dir / "versions"
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.storage_dir / "created_agents.json"
        self._lock = threading.RLock()
        self._agents: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._agents = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                self._agents = {}

    def _persist(self) -> None:
        try:
            self._path.write_text(json.dumps(self._agents, indent=2), encoding="utf-8")
        except Exception:
            pass  # aegis:allow-silent

    def register(self, name: str, blueprint_id: str, files: Dict[str, str],
                certification: Dict[str, Any], parent_version: Optional[int] = None) -> Dict[str, Any]:
        with self._lock:
            rec = self._agents.get(name)
            version = (rec["version"] + 1) if rec else 1
            # snapshot files for rollback
            snap_dir = self.versions_dir / name / f"v{version}"
            snap_dir.mkdir(parents=True, exist_ok=True)
            for rel, content in files.items():
                fp = snap_dir / rel.replace("/", "__")
                fp.write_text(content, encoding="utf-8")
            entry = {"name": name, "version": version, "blueprint_id": blueprint_id,
                    "parent_version": parent_version, "certified": certification.get("certified"),
                    "created_at": time.time(), "identity_id": (rec or {}).get("identity_id")
                                                              or f"agent-{uuid.uuid4().hex[:10]}",
                    "lifecycle_status": "deployed", "file_count": len(files),
                    "snapshot": str(snap_dir)}
            if rec:
                entry.setdefault("history", rec.get("history", []))
                entry["history"] = rec.get("history", []) + [{"version": rec["version"],
                                                             "retired_at": time.time()}]
            self._agents[name] = entry
            self._persist()
            return entry

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        return self._agents.get(name)

    def list_agents(self) -> List[Dict[str, Any]]:
        return list(self._agents.values())

    def rollback(self, name: str, to_version: int, target_dir: Path) -> Dict[str, Any]:
        """Restore a prior version's files into target_dir."""
        with self._lock:
            snap = self.versions_dir / name / f"v{to_version}"
            if not snap.exists():
                return {"status": "error", "message": f"no snapshot v{to_version} for {name}"}
            restored = []
            for fp in snap.iterdir():
                rel = fp.name.replace("__", "/")
                dest = target_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(fp.read_text(encoding="utf-8"), encoding="utf-8")
                restored.append(rel)
            return {"status": "complete", "restored": restored, "version": to_version}

    def retire(self, name: str, reason: str = "") -> Dict[str, Any]:
        with self._lock:
            rec = self._agents.get(name)
            if not rec:
                return {"status": "error", "message": "unknown agent"}
            rec["lifecycle_status"] = "retired"
            rec["retired_at"] = time.time()
            rec["retirement_reason"] = reason
            self._persist()
            return {"status": "complete", "retired": name, "knowledge_preserved": True}

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            statuses: Dict[str, int] = {}
            for a in self._agents.values():
                statuses[a.get("lifecycle_status", "?")] = statuses.get(a.get("lifecycle_status", "?"), 0) + 1
            return {"total_created": len(self._agents), "by_status": statuses,
                   "agents": [{"name": a["name"], "version": a["version"],
                             "status": a.get("lifecycle_status")} for a in self._agents.values()]}
