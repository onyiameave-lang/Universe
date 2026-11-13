"""
Oracle.cio.learning_store
=========================
Persistent Self-Learning State Store (ChronicleBackedStore + JSON) for
Oracle V2 CIO and its 3 Sub-Agents (Scout, Portfolio, Risk).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from scout_agent.models import SCOUT_DIMENSIONS  # type: ignore
except ImportError:
    from Oracle.scout_agent.models import SCOUT_DIMENSIONS  # type: ignore

_ORACLE_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_STATE_PATH = _ORACLE_ROOT / "memory" / "oracle_v2_learned_state.json"


class OracleV2LearningStore:
    """
    Manages the persistent empirical distributions for Scout, Portfolio, and Risk
    sub-agents. All parameters start from mathematical maximum-entropy / neutral
    symmetry priors and evolve continuously from realized trades and counterfactual
    replays.
    """

    def __init__(
        self,
        state_path: Optional[Path | str] = None,
        storage_dir: Optional[Path | str] = None,
        chronicle_client: Any = None,
    ) -> None:
        if state_path is not None:
            resolved_path = Path(state_path)
        elif storage_dir is not None:
            resolved_path = Path(storage_dir) / "oracle_v2_learned_state.json"
        else:
            resolved_path = _DEFAULT_STATE_PATH
        self.state_path = resolved_path
        self._lock = threading.RLock()
        self._store = None
        try:
            from shared.chronicle_sync import ChronicleBackedStore  # type: ignore
            self._store = ChronicleBackedStore(
                chronicle_client,
                "oracle_v2_learned_state",
                self.state_path,
                owner="oracle",
            )
        except Exception:
            self._store = None
        self._local_cache: Dict[str, Any] = {}
        self._ensure_initialized()

    @staticmethod
    def _initial_state() -> Dict[str, Any]:
        k_dims = len(SCOUT_DIMENSIONS)
        uniform_w = round(1.0 / k_dims, 6)
        return {
            "schema": "oracle.v2.learned_state.v1",
            "updated_at": time.time(),
            "scout": {
                "dimension_alpha": {d: 1.0 for d in SCOUT_DIMENSIONS},
                "dimension_beta": {d: 1.0 for d in SCOUT_DIMENSIONS},
                "dimension_weights": {d: uniform_w for d in SCOUT_DIMENSIONS},
                "conviction_outcomes": [],
                "counterfactual_outcomes": [],
                "learned_conviction_floor": 0.50,
                "symbol_regime_stats": {},
                "session_stats": {},
            },
            "portfolio": {
                "concurrency_outcomes": {
                    str(k): {"trades": 0, "wins": 0, "sum_r": 0.0, "sum_r2": 0.0}
                    for k in range(1, 10)
                },
                "daily_sequence_outcomes": {
                    str(m): {"trades": 0, "wins": 0, "sum_r": 0.0}
                    for m in range(1, 25)
                },
                "asset_class_stats": {
                    ac: {"trades": 0, "wins": 0, "sum_r": 0.0, "sum_r2": 0.0}
                    for ac in ("fx", "metals", "energy", "crypto", "indices")
                },
                "realized_trades": 0,
                "realized_wins": 0,
                "sum_win_r": 0.0,
                "sum_loss_r": 0.0,
                "sum_r": 0.0,
                "sum_r2": 0.0,
                "session_pnl_fractions": [],
            },
            "risk": {
                "overlap_outcomes": [],
                "reentry_outcomes": [],
                "news_event_outcomes": {
                    "medium": {"events": 0, "adverse_moves": 0, "favorable_moves": 0},
                    "high": {"events": 0, "adverse_moves": 0, "favorable_moves": 0},
                },
                "social_event_outcomes": {
                    "medium": {"events": 0, "adverse_moves": 0, "favorable_moves": 0},
                    "high": {"events": 0, "adverse_moves": 0, "favorable_moves": 0},
                },
                "geometry_outcomes": [],
            },
        }

    def _ensure_initialized(self) -> None:
        with self._lock:
            data = self.data
            defaults = self._initial_state()
            changed = False
            for k, v in defaults.items():
                if k not in data:
                    data[k] = v
                    changed = True
                elif isinstance(v, dict) and isinstance(data.get(k), dict):
                    for sub_k, sub_v in v.items():
                        if sub_k not in data[k]:
                            data[k][sub_k] = sub_v
                            changed = True
            if changed:
                self.save()

    @property
    def data(self) -> Dict[str, Any]:
        if self._store is not None:
            return self._store.data()
        if not self._local_cache:
            if self.state_path.exists():
                try:
                    loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        self._local_cache = loaded
                except Exception:
                    self._local_cache = {}
        return self._local_cache

    def save(self) -> None:
        with self._lock:
            self.data["updated_at"] = time.time()
            if self._store is not None:
                self._store.save()
            else:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.state_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
                tmp.replace(self.state_path)
