"""
Forge V2 - Challenger System (Phase 4A)
========================================
Manages champion positions, challenger requests, and competition results.

Principle:
  Evaluator measures. Benchmark contextualizes. Orchestration decides.
  Challenging ≠ Promotion.

The Challenger System:
  - Maintains a ChampionRegistry of incumbent positions and their history.
  - Accepts ChallengeRequests that contain ExperimentContracts.
  - Validates challenge scope against the registry (no silent corrections).
  - Produces immutable ChallengeResults with factual competition outcomes.
  - Does NOT auto-promote challengers to champion.
  - Does NOT recommend promotion.
  - Does NOT mutate ModelRegistry, HypothesisQueue, or any ecosystem state.

Phase 4D (Promotion Gate) alone can change champion state.
"""

import json
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from Forge.core.experimentation import ExperimentContract
from Forge.core.storage_paths import migrate_legacy_storage

if TYPE_CHECKING:
    from Forge.core.promotion import CommitResult


# ==============================================================================
# 1. PERSISTENCE CONFIGURATION
# ==============================================================================
_FORGE_ROOT = Path(__file__).resolve().parents[1]
CHALLENGER_DIR = _FORGE_ROOT / "memory" / "challenger"
CHAMPIONS_FILE = CHALLENGER_DIR / "champions.json"
CHALLENGES_FILE = CHALLENGER_DIR / "challenges.json"
RESULTS_FILE = CHALLENGER_DIR / "challenge_results.json"

CHALLENGER_DIR.mkdir(parents=True, exist_ok=True)
migrate_legacy_storage(
    _FORGE_ROOT / "challenger", CHALLENGER_DIR,
    ("champions.json", "champions.sqlite3", "challenges.json", "challenge_results.json",
     "challenges.sqlite3"),
)


_KNOWN_DOMAIN_OWNERS = {
    "ML_SUPERVISED": "forge",
    "ORACLE_STRATEGY": "oracle",
    "CHRONICLE_MEMORY": "chronicle",
    "PROMPT_OPTIMIZATION": "forge",
    "WORKFLOW_OPTIMIZATION": "forge",
    "CONFIG_OPTIMIZATION": "forge",
    "EVALUATION_ONLY": "forge",
}


def default_owner_for_domain(domain: str) -> Optional[str]:
    """Resolve only established domain ownership; custom domains must declare it."""
    return _KNOWN_DOMAIN_OWNERS.get(domain)


def normalize_domain_owners(domain_owners: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Combine built-in ownership with explicit operator mappings for new domains."""
    owners = dict(_KNOWN_DOMAIN_OWNERS)
    for domain, owner in (domain_owners or {}).items():
        if (not isinstance(domain, str) or not domain.strip()
                or not isinstance(owner, str) or not owner.strip()):
            raise ValueError("domain ownership entries require non-empty domain and owner strings")
        domain, owner = domain.strip(), owner.strip()
        built_in_owner = _KNOWN_DOMAIN_OWNERS.get(domain)
        if built_in_owner is not None and owner != built_in_owner:
            raise ValueError(f"Built-in domain {domain} is owned by {built_in_owner}")
        owners[domain] = owner
    return owners


# ==============================================================================
# 2. ENUMS
# ==============================================================================
class ChallengeStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


# ==============================================================================
# 3. CHAMPION TENURE (Historical Record)
# ==============================================================================
class ChampionTenure(BaseModel):
    """
    A single candidate's tenure as champion.
    Immutable historical record. Never overwritten.

    retirement_reason describes WHAT HAPPENED, not a promotion decision.
    Good: "challenged_by_CH-002", "manual_retirement", "superseded_by_new_champion"
    Bad: "challenger_was_better", "safe_to_promote"
    """
    model_config = ConfigDict(frozen=True)

    tenure_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    candidate_id: str                          # The artifact that held the position
    source_experiment_id: Optional[str] = None # The experiment that produced it
    source_challenge_id: Optional[str] = None  # The challenge that promoted it (if applicable)

    candidate_config: Dict[str, Any] = Field(default_factory=dict)   # Snapshot of configuration
    evidence_reference: Dict[str, Any] = Field(default_factory=dict) # Benchmark/evaluation provenance

    promoted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    retired_at: Optional[datetime] = None      # Set when a new candidate replaces this one
    retirement_reason: Optional[str] = None    # Describes what happened, not a decision


# ==============================================================================
# 4. CHAMPION RECORD (Registry Entry)
# ==============================================================================
class ChampionRecord(BaseModel):
    """
    A champion position in the ChampionRegistry.
    Contains the current incumbent and the full history of previous candidates.

    Invariant:
      current_tenure = active incumbent
      history = retired incumbents

    The current candidate is derived from current_tenure.candidate_id.
    There is no separate current_candidate_id field to avoid integrity problems.
    """
    champion_id: str                           # Stable identity: "CH-001"
    domain: str                                # e.g., "ML_SUPERVISED", "ORACLE_STRATEGY"
    context_key: str                           # Competition arena scope
    owner_agent: str = "legacy_unassigned"

    # Current incumbent (None if no champion has been established yet)
    current_tenure: Optional[ChampionTenure] = None

    # Full history (append-only, never destroyed)
    history: List[ChampionTenure] = Field(default_factory=list)

    # Registry metadata
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    revision: int = 0

    @model_validator(mode="before")
    @classmethod
    def _migrate_owner(cls, value):
        if isinstance(value, dict) and not value.get("owner_agent"):
            value = dict(value)
            value["owner_agent"] = default_owner_for_domain(value.get("domain", "")) or "legacy_unassigned"
        return value

    @field_validator("owner_agent")
    @classmethod
    def _valid_owner(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("champion owner_agent must be a non-empty agent identity")
        return value.strip()

    @property
    def current_candidate_id(self) -> Optional[str]:
        """Derived from current_tenure. No separate field to avoid inconsistency."""
        return self.current_tenure.candidate_id if self.current_tenure else None


class ChampionBootstrapRequest(BaseModel):
    """Persisted, evidence-bound proposal to establish an initial V2 incumbent."""

    model_config = ConfigDict(frozen=True)

    bootstrap_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    champion_id: str
    domain: str
    context_key: str
    owner_agent: str
    candidate_id: str
    candidate_config: Dict[str, Any] = Field(default_factory=dict)
    experiment_id: str
    run_id: str
    result_id: str
    metrics: Dict[str, float]
    metric_bootstrap: Dict[str, List[float]] = Field(default_factory=dict)
    bootstrap_evidence: Dict[str, Any] = Field(default_factory=dict)
    dataset_fingerprint: Optional[str] = None
    expected_revision: int
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="before")
    @classmethod
    def _migrate_owner(cls, value):
        if isinstance(value, dict) and not value.get("owner_agent"):
            value = dict(value)
            value["owner_agent"] = default_owner_for_domain(value.get("domain", "")) or ""
        return value

    @field_validator("owner_agent")
    @classmethod
    def _valid_owner(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("bootstrap owner_agent must be a non-empty agent identity")
        return value.strip()

    @field_validator("metrics", mode="before")
    @classmethod
    def _finite_metrics(cls, value):
        from Forge.core.metrics import finite_metric_value

        if not isinstance(value, dict) or not value:
            raise ValueError("bootstrap requires a non-empty metric snapshot")
        normalized = {}
        for name, raw_value in value.items():
            metric_value = finite_metric_value(raw_value)
            if not isinstance(name, str) or not name.strip() or metric_value is None:
                raise ValueError("bootstrap metrics must have names and finite numeric values")
            normalized[name] = metric_value
        return normalized

    @field_validator("metric_bootstrap", mode="before")
    @classmethod
    def _finite_metric_bootstrap(cls, value):
        from Forge.core.metrics import finite_metric_value

        if not isinstance(value, dict):
            raise ValueError("metric_bootstrap must map metric names to numeric draws")
        normalized = {}
        for name, draws in value.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(draws, list):
                raise ValueError("metric_bootstrap entries require metric names and draw lists")
            finite = [finite_metric_value(draw) for draw in draws]
            if len(finite) < 200 or any(draw is None for draw in finite):
                raise ValueError("bootstrap evidence requires at least 200 finite draws per metric")
            normalized[name] = finite
        return normalized


# ==============================================================================
# 5. CHALLENGE REQUEST (The Challenger's Declaration)
# ==============================================================================
class ChallengeRequest(BaseModel):
    """
    A formal request to challenge an incumbent champion.
    Contains the experiment that will produce the scientific evidence.

    Immutable once created. If something needs to change, create a new challenge_id.

    domain and context_key are DECLARATIONS TO VALIDATE, not authoritative copies.
    The authoritative scope is in the ChampionRegistry.
    The Challenger Engine validates: requested scope == champion scope.
    Mismatches are rejected, never silently corrected.
    """
    model_config = ConfigDict(frozen=True)

    challenge_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    # What is being challenged
    champion_id: str                           # Must exist in ChampionRegistry

    # Competition scope (validated against the champion's registry entry)
    domain: str
    context_key: str
    owner_agent: str = "forge"

    # The candidate challenging the champion
    candidate_id: str                          # The artifact/config being proposed
    candidate_config: Dict[str, Any] = Field(default_factory=dict)

    # The experiment that will produce evidence (CONTAINED, not referenced)
    experiment: ExperimentContract

    # Provenance
    created_by: str                            # e.g., "Forge.EvolveLoop", "Oracle.PortfolioSelector"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="before")
    @classmethod
    def _resolve_owner(cls, value):
        if isinstance(value, dict) and not value.get("owner_agent"):
            value = dict(value)
            value["owner_agent"] = default_owner_for_domain(value.get("domain", "")) or ""
        return value

    @field_validator("owner_agent")
    @classmethod
    def _valid_owner(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("challenge owner_agent must be a non-empty agent identity")
        return value.strip()


# ==============================================================================
# 6. CHALLENGE RESULT (Factual Output)
# ==============================================================================
class ChallengeResult(BaseModel):
    """
    The factual outcome of a challenger match.
    Immutable. Does NOT recommend promotion. Does NOT mutate champion state.

    Contains references to the scientific evidence produced by the
    Experiment → Evaluator → Benchmark pipeline.

    Authoritative metric values live in the BenchmarkReport.
    This result only carries lightweight summary lists for quick indexing.
    """
    model_config = ConfigDict(frozen=True)

    challenge_id: str

    # Identity: who competed
    champion_id: str
    candidate_id: str

    # Experiment lineage
    experiment_id: str
    run_id: str
    result_id: str

    # Scientific evidence (references, not full copies)
    evaluation_report_id: Optional[str] = None   # Links to EvaluationReport
    benchmark_report_id: Optional[str] = None     # Links to BenchmarkReport

    # Key comparison facts (lightweight summary extracted from reports)
    metrics_better_than_champion: List[str] = Field(default_factory=list)
    metrics_worse_than_champion: List[str] = Field(default_factory=list)
    metrics_unchanged: List[str] = Field(default_factory=list)
    metrics_incomparable: List[str] = Field(default_factory=list)

    # Status
    status: ChallengeStatus
    error_message: Optional[str] = None
    reconciliation_evidence: Dict[str, Any] = Field(default_factory=dict)

    # Provenance
    challenger_version: str = "4.0.0"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ChallengeReconciliationAuthorization(BaseModel):
    """Aegis-signed authorization to close a challenge abandoned pre-run."""

    model_config = ConfigDict(frozen=True)

    authorization_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    challenge_id: str
    authorized_by: str
    decision: str = "AUTHORIZED"
    audience: str = "forge.operations"
    issuer: str
    issued_at: datetime
    expires_at: datetime
    nonce: str
    signature: str
    claims: Dict[str, Any]


# ==============================================================================
# 7. PERSISTENCE HELPERS
# ==============================================================================
def _load_json(file_path: Path) -> Dict[str, Any]:
    if file_path.exists():
        with open(file_path, "r") as f:
            return json.load(f)
    return {}


def _save_json(file_path: Path, data: Dict[str, Any]) -> None:
    with open(file_path, "w") as f:
        json.dump(data, f, indent=2, default=str)


# ==============================================================================
# 8. CHAMPION REGISTRY
# ==============================================================================
class ChampionRegistry:
    """
    Manages champion positions and their history.

    The ChampionRegistry:
      - Registers champion positions (domain + context_key).
      - Tracks the current incumbent and full tenure history.
      - Validates ChallengeRequests against registered champions.
      - Does NOT auto-promote. Does NOT run experiments.
      - Does NOT make promotion decisions.

    Phase 4D (Promotion Gate) alone can change champion state.
    """

    def __init__(self, domain_owners: Optional[Dict[str, str]] = None,
                 authorization_verifier=None):
        self._domain_owners = normalize_domain_owners(domain_owners)
        self._authorization_verifier = authorization_verifier
        self._champions: Dict[str, ChampionRecord] = {}
        self._committed_challenges = set()
        self._lock = threading.RLock()
        self._active_connection: Optional[sqlite3.Connection] = None
        # Keep the historical JSON path as the migration source and place the
        # transactional registry beside it. New writes go only to SQLite.
        self._database_path = CHAMPIONS_FILE.with_suffix(".sqlite3")
        self._initialize_database()
        self._load_state()

    def configure_authorization_verifier(self, verifier) -> None:
        """Install the trusted verifier once and reject silent verifier replacement."""
        if verifier is None:
            return
        with self._lock:
            if self._authorization_verifier is None:
                self._authorization_verifier = verifier
            elif self._authorization_verifier is not verifier:
                raise RuntimeError("ChampionRegistry authorization verifier is already configured")

    def _connect(self) -> sqlite3.Connection:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self._database_path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize_database(self) -> None:
        """Create the transactional schema and import legacy JSON exactly once."""
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS registry_metadata "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS champions "
                "(champion_id TEXT PRIMARY KEY, record_json TEXT NOT NULL, "
                "revision INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS committed_challenges "
                "(challenge_id TEXT PRIMARY KEY, committed_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS promotion_decisions "
                "(decision_id TEXT PRIMARY KEY, challenge_id TEXT NOT NULL, "
                "decision_json TEXT NOT NULL, status TEXT NOT NULL, "
                "commit_result_json TEXT, created_at TEXT NOT NULL, committed_at TEXT)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS promotion_attempts "
                "(attempt_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, "
                "challenge_id TEXT NOT NULL, champion_id TEXT NOT NULL, "
                "authorization_id TEXT, authorization_consumed INTEGER NOT NULL "
                "CHECK(authorization_consumed IN (0, 1)), failure_reason TEXT NOT NULL, "
                "failure_detail TEXT NOT NULL, attempted_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS promotion_attempts_decision_idx "
                "ON promotion_attempts(decision_id, attempted_at)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS champion_bootstraps "
                "(bootstrap_id TEXT PRIMARY KEY, champion_id TEXT NOT NULL REFERENCES champions(champion_id), "
                "request_json TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT, "
                "created_at TEXT NOT NULL, committed_at TEXT)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS champion_bootstrap_attempts "
                "(attempt_id TEXT PRIMARY KEY, bootstrap_id TEXT NOT NULL, authorization_id TEXT, "
                "authorization_consumed INTEGER NOT NULL CHECK(authorization_consumed IN (0, 1)), "
                "failure_reason TEXT NOT NULL, failure_detail TEXT NOT NULL, attempted_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS champion_bootstrap_attempts_time_idx "
                "ON champion_bootstrap_attempts(attempted_at)"
            )
            connection.execute("BEGIN IMMEDIATE")
            migrated = connection.execute(
                "SELECT value FROM registry_metadata WHERE key = ?",
                ("legacy_json_migrated",),
            ).fetchone()
            if migrated is None:
                # Invalid legacy JSON raises and prevents startup from silently
                # replacing or ignoring the existing registry.
                legacy_champions = _load_json(CHAMPIONS_FILE)
                legacy_commits = _load_json(CHALLENGER_DIR / "committed_challenges.json")
                if not isinstance(legacy_champions, dict):
                    raise ValueError("Legacy champion registry must be a JSON object.")
                if not isinstance(legacy_commits, dict):
                    raise ValueError("Legacy committed challenge ledger must be a JSON object.")

                for champion_id, raw_record in legacy_champions.items():
                    record_data = dict(raw_record)
                    record_data.setdefault("revision", 0)
                    record = ChampionRecord(**record_data)
                    self._write_champion(connection, record)

                committed_ids = legacy_commits.get("committed", [])
                if not isinstance(committed_ids, list) or any(
                    not isinstance(item, str) or not item for item in committed_ids
                ):
                    raise ValueError("Legacy committed challenge IDs are invalid.")
                connection.executemany(
                    "INSERT OR IGNORE INTO committed_challenges "
                    "(challenge_id, committed_at) VALUES (?, ?)",
                    [(challenge_id, datetime.now(timezone.utc).isoformat())
                     for challenge_id in committed_ids],
                )
                connection.execute(
                    "INSERT INTO registry_metadata (key, value) VALUES (?, ?)",
                    ("legacy_json_migrated", "1"),
                )
            saved_owners = {
                row["key"].removeprefix("domain_owner:"): row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM registry_metadata WHERE key GLOB 'domain_owner:*'"
                ).fetchall()
            }
            for domain, saved_owner in saved_owners.items():
                if self._domain_owners.get(domain) != saved_owner:
                    raise RuntimeError(
                        f"Domain owner mapping for {domain} changed or is missing; "
                        "restore the persisted mapping or use a governed ownership change"
                    )
            connection.executemany(
                "INSERT INTO registry_metadata (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO NOTHING",
                [(f"domain_owner:{domain}", owner)
                 for domain, owner in self._domain_owners.items()],
            )
            connection.commit()

    def owner_for_domain(self, domain: str) -> Optional[str]:
        return self._domain_owners.get(domain)

    def configure_domain_owners(self, domain_owners: Dict[str, str]) -> None:
        """Add operator mappings without allowing persisted owner changes or removals."""
        proposed = normalize_domain_owners({**self._domain_owners, **domain_owners})
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                saved = {
                    row["key"].removeprefix("domain_owner:"): row["value"]
                    for row in connection.execute(
                        "SELECT key, value FROM registry_metadata WHERE key GLOB 'domain_owner:*'"
                    ).fetchall()
                }
                for domain, owner in saved.items():
                    if proposed.get(domain) != owner:
                        raise RuntimeError(
                            f"Persisted owner for {domain} cannot be changed through configuration"
                        )
                connection.executemany(
                    "INSERT INTO registry_metadata (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO NOTHING",
                    [(f"domain_owner:{domain}", owner)
                     for domain, owner in proposed.items()],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._domain_owners = proposed

    def ownership_status(self) -> Dict[str, Any]:
        issues = []
        for champion in self.list_champions():
            expected = self.owner_for_domain(champion.domain)
            if expected is None:
                issues.append({
                    "champion_id": champion.champion_id,
                    "domain": champion.domain,
                    "issue": "domain_owner_not_configured",
                })
            elif champion.owner_agent != expected:
                issues.append({
                    "champion_id": champion.champion_id,
                    "domain": champion.domain,
                    "stored_owner": champion.owner_agent,
                    "configured_owner": expected,
                    "issue": "champion_owner_mismatch",
                })
        return {
            "ready": not issues,
            "domain_owners": dict(sorted(self._domain_owners.items())),
            "issues": issues,
        }

    @staticmethod
    def _write_champion(
        connection: sqlite3.Connection,
        record: ChampionRecord,
    ) -> None:
        payload = json.dumps(record.model_dump(mode="json"), sort_keys=True)
        connection.execute(
            "INSERT INTO champions (champion_id, record_json, revision) "
            "VALUES (?, ?, ?) ON CONFLICT(champion_id) DO UPDATE SET "
            "record_json = excluded.record_json, revision = excluded.revision",
            (record.champion_id, payload, record.revision),
        )

    def _load_state(self):
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT champion_id, record_json FROM champions"
            ).fetchall()
            committed_rows = connection.execute(
                "SELECT challenge_id FROM committed_challenges"
            ).fetchall()
        self._champions = {
            row["champion_id"]: ChampionRecord.model_validate(
                json.loads(row["record_json"])
            )
            for row in rows
        }
        self._committed_challenges = {row["challenge_id"] for row in committed_rows}

    def _save_committed_challenges(self):
        """Persist commit markers in the current transaction or a new one."""
        connection = self._active_connection
        owns_connection = connection is None
        if owns_connection:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executemany(
                "INSERT OR IGNORE INTO committed_challenges "
                "(challenge_id, committed_at) VALUES (?, ?)",
                [(challenge_id, datetime.now(timezone.utc).isoformat())
                 for challenge_id in self._committed_challenges],
            )
            if owns_connection:
                connection.commit()
        except Exception:
            if owns_connection:
                connection.rollback()
            raise
        finally:
            if owns_connection:
                connection.close()

    def register_champion(
        self, domain: str, context_key: str, owner_agent: Optional[str] = None
    ) -> ChampionRecord:
        """
        Registers a new champion position.
        A champion position starts with no incumbent (current_tenure=None).
        """
        owner = self.owner_for_domain(domain)
        if owner is None:
            raise ValueError(f"Domain {domain!r} has no operator-configured owner")
        if owner_agent is not None and (
            not isinstance(owner_agent, str) or owner_agent.strip() != owner
        ):
            raise PermissionError(f"Domain {domain} is owned by {owner}, not {owner_agent}")
        champion_id = f"CH-{uuid.uuid4().hex[:8]}"
        record = ChampionRecord(
            champion_id=champion_id,
            domain=domain,
            context_key=context_key,
            owner_agent=owner.strip(),
        )
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._write_champion(connection, record)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
            self._champions[champion_id] = record.model_copy(deep=True)
        return record.model_copy(deep=True)

    def initialize_champion(
        self,
        champion_id: str,
        founding_tenure: ChampionTenure,
        expected_revision: int = 0,
    ) -> ChampionRecord:
        """Disabled: founding incumbents must pass the signed bootstrap gate."""
        raise PermissionError(
            "Direct champion initialization is disabled. Persist a ChampionBootstrapRequest, "
            "verify Aegis authorization, and commit it through the governed bootstrap path."
        )

    def get_champion(self, champion_id: str) -> Optional[ChampionRecord]:
        """Retrieves the latest persisted champion record by ID."""
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT record_json FROM champions WHERE champion_id = ?",
                (champion_id,),
            ).fetchone()
            if row is None:
                self._champions.pop(champion_id, None)
                return None
            record = ChampionRecord.model_validate(json.loads(row["record_json"]))
            self._champions[champion_id] = record.model_copy(deep=True)
            return record.model_copy(deep=True)

    def get_current_candidate(self, champion_id: str) -> Optional[str]:
        """Derives the current candidate from the current tenure."""
        record = self.get_champion(champion_id)
        if record is None:
            return None
        return record.current_candidate_id

    def get_history(self, champion_id: str) -> List[ChampionTenure]:
        """Returns the full tenure history for a champion position."""
        record = self.get_champion(champion_id)
        if record is None:
            return []
        return list(record.history)

    def validate_challenge(self, request: ChallengeRequest) -> Optional[str]:
        """
        Validates a ChallengeRequest against the registry.

        Returns None if valid, or an error message string if invalid.

        Validation rules:
          1. champion_id must exist in the registry.
          2. request.domain must match champion.domain.
          3. request.context_key must match champion.context_key.
          4. request.experiment.domain must be compatible.

        Mismatches are REJECTED, never silently corrected.
        """
        record = self.get_champion(request.champion_id)
        if record is None:
            return f"Champion '{request.champion_id}' not found in registry."

        if request.domain != record.domain:
            return (
                f"Domain mismatch: request declares '{request.domain}' "
                f"but champion '{request.champion_id}' is scoped to '{record.domain}'. "
                f"Challenge rejected."
            )

        if request.context_key != record.context_key:
            return (
                f"Context mismatch: request declares '{request.context_key}' "
                f"but champion '{request.champion_id}' is scoped to '{record.context_key}'. "
                f"Challenge rejected."
            )

        configured_owner = self.owner_for_domain(record.domain)
        if (configured_owner is None or record.owner_agent != configured_owner
                or request.owner_agent != configured_owner):
            return (
                f"Owner mismatch or missing policy: domain '{record.domain}' is configured for "
                f"'{configured_owner}', slot owner is '{record.owner_agent}', request owner is "
                f"'{request.owner_agent}'. Challenge rejected."
            )

        return None  # Valid

    def list_champions(self, domain: Optional[str] = None) -> List[ChampionRecord]:
        """Lists all registered champions, optionally filtered by domain."""
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT champion_id, record_json FROM champions ORDER BY champion_id"
            ).fetchall()
            champions = [
                ChampionRecord.model_validate(json.loads(row["record_json"]))
                for row in rows
            ]
            self._champions = {
                record.champion_id: record.model_copy(deep=True)
                for record in champions
            }
            if domain is not None:
                champions = [record for record in champions if record.domain == domain]
            return [record.model_copy(deep=True) for record in champions]

    def promote(
        self,
        champion_id: str,
        new_tenure: ChampionTenure,
        expected_incumbent_tenure_id: str,
        expected_revision: int,
        expected_domain: str,
        expected_context_key: str,
        retirement_reason: str,
        challenge_id: str,
        expected_owner_agent: Optional[str] = None,
        promotion_decision_id: Optional[str] = None,
        authorization=None,
    ) -> "CommitResult":
        """Atomically replaces an incumbent and consumes its persisted decision."""
        from Forge.core.promotion import (
            CommitFailureReason,
            CommitResult,
            PromotionOutcome,
        )

        with self._lock:
            connection = self._connect()
            original_committed = set(self._committed_challenges)
            updated_record = None
            retired_tenure = None
            authorization_consumed = False
            authorization_id = authorization.authorization_id if authorization is not None else None
            try:
                # BEGIN IMMEDIATE serializes promotion writers across processes.
                # Champion state and the replay-prevention marker commit together.
                connection.execute("BEGIN IMMEDIATE")

                def reject(reason, detail):
                    connection.rollback()
                    failure_audit_recorded = False
                    try:
                        self.record_promotion_attempt(
                            decision_id=promotion_decision_id or f"unpersisted:{uuid.uuid4()}",
                            challenge_id=challenge_id,
                            champion_id=champion_id,
                            authorization_id=(authorization.authorization_id
                                              if authorization is not None else None),
                            authorization_consumed=authorization_consumed,
                            failure_reason=reason.value,
                            failure_detail=detail,
                        )
                        failure_audit_recorded = True
                    except Exception:
                        # The caller receives an explicit audit flag and can
                        # retry persistence at the gate boundary.
                        pass
                    return CommitResult(
                        success=False,
                        challenge_id=challenge_id,
                        champion_id=champion_id,
                        outcome=PromotionOutcome.BLOCKED,
                        failure_reason=reason,
                        failure_detail=detail,
                        authorization_id=(authorization.authorization_id
                                          if authorization is not None else None),
                        authorization_consumed=authorization_consumed,
                        failure_audit_recorded=failure_audit_recorded,
                    )

                if promotion_decision_id is None:
                    return reject(
                        CommitFailureReason.DECISION_NOT_COMMITTABLE,
                        "Champion transitions require a persisted PromotionGate decision.",
                    )

                duplicate = connection.execute(
                    "SELECT 1 FROM committed_challenges WHERE challenge_id = ?",
                    (challenge_id,),
                ).fetchone()
                if duplicate is not None:
                    return reject(
                        CommitFailureReason.DUPLICATE_COMMIT,
                        f"Challenge '{challenge_id}' has already been committed.",
                    )

                decision_row = None
                if promotion_decision_id is not None:
                    decision_row = connection.execute(
                        "SELECT decision_json, status, challenge_id FROM promotion_decisions "
                        "WHERE decision_id = ?",
                        (promotion_decision_id,),
                    ).fetchone()
                    if (
                        decision_row is None
                        or decision_row["status"] != "EVALUATED"
                        or decision_row["challenge_id"] != challenge_id
                    ):
                        return reject(
                            CommitFailureReason.DECISION_NOT_COMMITTABLE,
                            "Persisted promotion decision is missing, mismatched, or already consumed.",
                        )
                    persisted_decision = json.loads(decision_row["decision_json"])
                    if (
                        persisted_decision.get("decision_id") != promotion_decision_id
                        or persisted_decision.get("outcome") != "APPROVED"
                        or persisted_decision.get("champion_id") != champion_id
                        or persisted_decision.get("challenge_id") != challenge_id
                        or persisted_decision.get("expected_revision") != expected_revision
                        or persisted_decision.get("expected_incumbent_tenure_id") != expected_incumbent_tenure_id
                        or persisted_decision.get("expected_domain") != expected_domain
                        or persisted_decision.get("expected_context_key") != expected_context_key
                        or persisted_decision.get("expected_owner_agent") != expected_owner_agent
                    ):
                        return reject(
                            CommitFailureReason.DECISION_NOT_COMMITTABLE,
                            "Persisted promotion decision claims do not match the requested transition.",
                        )

                row = connection.execute(
                    "SELECT record_json FROM champions WHERE champion_id = ?",
                    (champion_id,),
                ).fetchone()
                if row is None:
                    return reject(
                        CommitFailureReason.CHAMPION_NOT_FOUND,
                        f"Champion '{champion_id}' not found in registry.",
                    )
                record = ChampionRecord.model_validate(json.loads(row["record_json"]))

                if record.domain != expected_domain or record.context_key != expected_context_key:
                    return reject(
                        CommitFailureReason.DOMAIN_CONTEXT_MISMATCH,
                        f"Expected domain/context '{expected_domain}'/'{expected_context_key}', "
                        f"found '{record.domain}'/'{record.context_key}'.",
                    )
                if (not expected_owner_agent or record.owner_agent == "legacy_unassigned"
                        or record.owner_agent != expected_owner_agent
                        or self.owner_for_domain(record.domain) != expected_owner_agent):
                    return reject(
                        CommitFailureReason.OWNER_MISMATCH,
                        "Champion ownership is unresolved or does not match the authorized owner.",
                    )
                if record.current_tenure is None:
                    return reject(
                        CommitFailureReason.STALE_INCUMBENT,
                        f"Champion '{champion_id}' has no current tenure.",
                    )
                if record.current_tenure.tenure_id != expected_incumbent_tenure_id:
                    return reject(
                        CommitFailureReason.STALE_INCUMBENT,
                        f"Expected incumbent tenure '{expected_incumbent_tenure_id}', "
                        f"found '{record.current_tenure.tenure_id}'.",
                    )
                if record.revision != expected_revision:
                    return reject(
                        CommitFailureReason.REGISTRY_REVISION_MISMATCH,
                        f"Expected registry revision {expected_revision}, "
                        f"found {record.revision}.",
                    )

                from Forge.core.promotion import PromotionDecision, PromotionGate
                decision_model = PromotionDecision.model_validate(persisted_decision)
                if not decision_model.authorization_required:
                    return reject(
                        CommitFailureReason.DECISION_NOT_COMMITTABLE,
                        "Persisted champion promotion decision does not require mandatory Aegis approval.",
                    )
                if authorization is None:
                    return reject(
                        CommitFailureReason.AUTHORIZATION_MISSING,
                        "Every champion transition requires signed Aegis authorization.",
                    )
                verifier = self._authorization_verifier
                if verifier is None:
                    return reject(
                        CommitFailureReason.AUTHORIZATION_DENIED,
                        "No trusted Aegis verifier is configured; promotion is disabled.",
                    )
                if (authorization.challenge_id != challenge_id
                        or authorization.audience != "forge.promotion"
                        or authorization.risk_category != decision_model.risk_category
                        or authorization.decision != "AUTHORIZED"
                        or not authorization.issuer or not authorization.signature
                        or not authorization.nonce):
                    return reject(
                        CommitFailureReason.AUTHORIZATION_DENIED,
                        "Authorization artifact does not match the persisted Forge promotion decision.",
                    )
                required_claims = PromotionGate.authorization_claims(
                    decision_model, authorization.authorized_by
                )
                if authorization.claims != required_claims:
                    return reject(
                        CommitFailureReason.AUTHORIZATION_DENIED,
                        "Authorization claims do not bind approval to the exact persisted decision.",
                    )
                try:
                    verified = verifier.verify_and_consume(authorization, required_claims)
                except Exception as exc:
                    return reject(
                        CommitFailureReason.AUTHORIZATION_DENIED,
                        f"Trusted authorization verification failed: {type(exc).__name__}.",
                    )
                if not verified:
                    return reject(
                        CommitFailureReason.AUTHORIZATION_DENIED,
                        "Trusted authorization verification rejected the artifact.",
                    )
                authorization_consumed = True
                authorization_id = authorization.authorization_id

                import copy

                retired_tenure = record.current_tenure
                retired_with_metadata = ChampionTenure(
                    tenure_id=retired_tenure.tenure_id,
                    candidate_id=retired_tenure.candidate_id,
                    source_experiment_id=retired_tenure.source_experiment_id,
                    source_challenge_id=retired_tenure.source_challenge_id,
                    candidate_config=copy.deepcopy(retired_tenure.candidate_config),
                    evidence_reference=copy.deepcopy(retired_tenure.evidence_reference),
                    promoted_at=retired_tenure.promoted_at,
                    retired_at=datetime.now(timezone.utc),
                    retirement_reason=retirement_reason,
                )
                updated_record = ChampionRecord(
                    champion_id=record.champion_id,
                    domain=record.domain,
                    context_key=record.context_key,
                    owner_agent=record.owner_agent,
                    current_tenure=new_tenure.model_copy(deep=True),
                    history=[*record.history, retired_with_metadata],
                    created_at=record.created_at,
                    last_updated=datetime.now(timezone.utc),
                    revision=record.revision + 1,
                )
                self._write_champion(connection, updated_record)
                self._committed_challenges.add(challenge_id)
                self._active_connection = connection
                try:
                    # Kept as an overridable persistence seam for failure
                    # injection; it writes into this same SQLite transaction.
                    self._save_committed_challenges()
                finally:
                    self._active_connection = None
                commit_result = CommitResult(
                    success=True,
                    challenge_id=challenge_id,
                    champion_id=champion_id,
                    outcome=PromotionOutcome.APPROVED,
                    authorization_id=authorization_id,
                    authorization_consumed=authorization_consumed,
                    new_tenure_id=updated_record.current_tenure.tenure_id,
                    retired_tenure_id=retired_tenure.tenure_id,
                    registry_revision=updated_record.revision,
                )
                if promotion_decision_id is not None:
                    connection.execute(
                        "UPDATE promotion_decisions SET status = 'COMMITTED', "
                        "commit_result_json = ?, committed_at = ? "
                        "WHERE decision_id = ? AND status = 'EVALUATED'",
                        (json.dumps(commit_result.model_dump(mode="json"), sort_keys=True,
                                    separators=(",", ":"), allow_nan=False),
                         datetime.now(timezone.utc).isoformat(), promotion_decision_id),
                    )
                    if connection.execute("SELECT changes()").fetchone()[0] != 1:
                        raise RuntimeError("Promotion decision was concurrently consumed")
                connection.commit()
                self._champions[champion_id] = updated_record
            except Exception as exc:
                connection.rollback()
                self._committed_challenges = original_committed
                failure_audit_recorded = False
                try:
                    self.record_promotion_attempt(
                        decision_id=promotion_decision_id or f"unpersisted:{uuid.uuid4()}",
                        challenge_id=challenge_id,
                        champion_id=champion_id,
                        authorization_id=authorization_id,
                        authorization_consumed=authorization_consumed,
                        failure_reason=CommitFailureReason.PERSISTENCE_FAILURE.value,
                        failure_detail=f"Registry transaction failed and was rolled back: {exc}",
                    )
                    failure_audit_recorded = True
                except Exception:
                    pass
                return CommitResult(
                    success=False,
                    challenge_id=challenge_id,
                    champion_id=champion_id,
                    outcome=PromotionOutcome.BLOCKED,
                    failure_reason=CommitFailureReason.PERSISTENCE_FAILURE,
                    failure_detail=f"Registry transaction failed and was rolled back: {exc}",
                    authorization_id=authorization_id,
                    authorization_consumed=authorization_consumed,
                    failure_audit_recorded=failure_audit_recorded,
                )
            finally:
                self._active_connection = None
                connection.close()

            return commit_result

    @staticmethod
    def _promotion_decision_json(decision) -> str:
        return json.dumps(
            decision.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    def record_promotion_decision(self, decision) -> None:
        """Persist an immutable policy result before it can be committed."""
        payload = self._promotion_decision_json(decision)
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT decision_json, challenge_id FROM promotion_decisions WHERE decision_id = ?",
                    (decision.decision_id,),
                ).fetchone()
                if row is not None:
                    if row["decision_json"] != payload or row["challenge_id"] != decision.challenge_id:
                        raise ValueError("Promotion decision ID already exists with different content")
                    connection.commit()
                    return
                connection.execute(
                    "INSERT INTO promotion_decisions "
                    "(decision_id, challenge_id, decision_json, status, created_at) "
                    "VALUES (?, ?, ?, 'EVALUATED', ?)",
                    (decision.decision_id, decision.challenge_id, payload,
                     datetime.now(timezone.utc).isoformat()),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def record_champion_bootstrap(self, request: ChampionBootstrapRequest) -> None:
        """Persist a proposed initial incumbent without changing champion state."""
        payload = request.model_dump_json()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT record_json FROM champions WHERE champion_id = ?",
                    (request.champion_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Champion '{request.champion_id}' is not registered")
                champion = ChampionRecord.model_validate_json(row["record_json"])
                if champion.domain != request.domain or champion.context_key != request.context_key:
                    raise ValueError("Bootstrap scope does not match the registered champion slot")
                if (champion.owner_agent == "legacy_unassigned"
                        or self.owner_for_domain(champion.domain) != request.owner_agent
                        or champion.owner_agent != request.owner_agent):
                    raise PermissionError("Bootstrap owner does not own the registered champion slot")
                if champion.current_tenure is not None:
                    raise ValueError("Cannot bootstrap a champion slot that already has an incumbent")
                if champion.revision != request.expected_revision:
                    raise ValueError("Champion revision changed before bootstrap was prepared")
                pending = connection.execute(
                    "SELECT bootstrap_id FROM champion_bootstraps "
                    "WHERE champion_id = ? AND status = 'PREPARED' LIMIT 1",
                    (request.champion_id,),
                ).fetchone()
                if pending is not None and pending["bootstrap_id"] != request.bootstrap_id:
                    raise ValueError(
                        f"Champion slot already has a prepared bootstrap: {pending['bootstrap_id']}"
                    )
                existing = connection.execute(
                    "SELECT request_json FROM champion_bootstraps WHERE bootstrap_id = ?",
                    (request.bootstrap_id,),
                ).fetchone()
                if existing is not None:
                    if existing["request_json"] != payload:
                        raise ValueError("Bootstrap ID already exists with different content")
                    connection.commit()
                    return
                connection.execute(
                    "INSERT INTO champion_bootstraps "
                    "(bootstrap_id, champion_id, request_json, status, created_at) "
                    "VALUES (?, ?, ?, 'PREPARED', ?)",
                    (request.bootstrap_id, request.champion_id, payload,
                     request.created_at.isoformat()),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def get_champion_bootstrap(self, bootstrap_id: str):
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT request_json, status, result_json FROM champion_bootstraps "
                "WHERE bootstrap_id = ?",
                (bootstrap_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "request": ChampionBootstrapRequest.model_validate_json(row["request_json"]),
            "status": row["status"],
            "result": ChampionRecord.model_validate_json(row["result_json"])
            if row["result_json"] else None,
        }

    def commit_champion_bootstrap(
        self,
        bootstrap_id: str,
        authorization,
    ) -> ChampionRecord:
        """Atomically install the authorized founding tenure and consume bootstrap."""
        authorization_consumed = False
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                bootstrap = connection.execute(
                    "SELECT request_json, status, result_json FROM champion_bootstraps "
                    "WHERE bootstrap_id = ?",
                    (bootstrap_id,),
                ).fetchone()
                if bootstrap is None:
                    raise KeyError(f"Bootstrap '{bootstrap_id}' was not found")
                if bootstrap["status"] == "COMMITTED":
                    connection.commit()
                    return ChampionRecord.model_validate_json(bootstrap["result_json"])
                if bootstrap["status"] != "PREPARED":
                    raise ValueError(f"Bootstrap is not committable: {bootstrap['status']}")
                request = ChampionBootstrapRequest.model_validate_json(bootstrap["request_json"])
                row = connection.execute(
                    "SELECT record_json FROM champions WHERE champion_id = ?",
                    (request.champion_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Champion '{request.champion_id}' was not registered")
                champion = ChampionRecord.model_validate_json(row["record_json"])
                if champion.domain != request.domain or champion.context_key != request.context_key:
                    raise ValueError("Champion scope changed after bootstrap authorization")
                if (champion.owner_agent == "legacy_unassigned"
                        or self.owner_for_domain(champion.domain) != request.owner_agent
                        or champion.owner_agent != request.owner_agent):
                    raise PermissionError("Champion ownership changed after bootstrap authorization")
                if champion.current_tenure is not None or champion.revision != request.expected_revision:
                    raise ValueError("Champion state changed after bootstrap authorization")
                other_rows = connection.execute(
                    "SELECT champion_id, record_json FROM champions WHERE champion_id != ?",
                    (champion.champion_id,),
                ).fetchall()
                if any(
                    (other := ChampionRecord.model_validate_json(other_row["record_json"])).domain == request.domain
                    and other.context_key == request.context_key
                    and other.current_tenure is not None
                    for other_row in other_rows
                ):
                    raise ValueError("Another incumbent now exists for this domain/context")

                from Forge.core.adapters.discovery_translator import DiscoveryTranslator
                required_claims = DiscoveryTranslator.bootstrap_authorization_claims(
                    request, authorization.authorized_by
                )
                verifier = self._authorization_verifier
                if verifier is None:
                    raise PermissionError(
                        "No trusted Aegis verifier is configured; champion bootstrap is disabled"
                    )
                if (authorization.audience != "forge.promotion"
                        or authorization.risk_category != "initialization"
                        or authorization.decision != "AUTHORIZED"
                        or not authorization.issuer or not authorization.signature
                        or not authorization.nonce
                        or authorization.claims != required_claims):
                    raise PermissionError(
                        "Authorization artifact does not bind approval to this exact champion bootstrap"
                    )
                if authorization.issued_at.tzinfo is None or (
                    authorization.expires_at is not None
                    and authorization.expires_at.tzinfo is None
                ):
                    raise PermissionError("Authorization timestamps must be timezone-aware")
                now = datetime.now(timezone.utc)
                if authorization.issued_at > now or (
                    authorization.expires_at is not None and now > authorization.expires_at
                ):
                    raise PermissionError("Champion bootstrap authorization is not currently valid")
                try:
                    verified = verifier.verify_and_consume(authorization, required_claims)
                except Exception as exc:
                    raise PermissionError(
                        f"Trusted Aegis verification failed: {type(exc).__name__}"
                    ) from exc
                if not verified:
                    raise PermissionError("Trusted Aegis authorization rejected the bootstrap")
                authorization_consumed = True

                tenure = ChampionTenure(
                    candidate_id=request.candidate_id,
                    source_experiment_id=request.experiment_id,
                    candidate_config=request.candidate_config,
                    evidence_reference={
                        "founding_metrics": request.metrics,
                        "metric_bootstrap": request.metric_bootstrap,
                        "bootstrap_evidence": request.bootstrap_evidence,
                        "bootstrap_id": request.bootstrap_id,
                        "experiment_id": request.experiment_id,
                        "run_id": request.run_id,
                        "result_id": request.result_id,
                        "dataset_fingerprint": request.dataset_fingerprint,
                        "owner_agent": request.owner_agent,
                        "authorization_id": authorization.authorization_id,
                        "authorization_issuer": authorization.issuer,
                        "authorized_by": authorization.authorized_by,
                    },
                )
                initialized = champion.model_copy(deep=True, update={
                    "current_tenure": tenure,
                    "last_updated": datetime.now(timezone.utc),
                    "revision": champion.revision + 1,
                })
                self._write_champion(connection, initialized)
                connection.execute(
                    "UPDATE champion_bootstraps SET status = 'COMMITTED', result_json = ?, committed_at = ? "
                    "WHERE bootstrap_id = ? AND status = 'PREPARED'",
                    (initialized.model_dump_json(), datetime.now(timezone.utc).isoformat(), bootstrap_id),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise RuntimeError("Bootstrap request was concurrently consumed")
                connection.commit()
                self._champions[initialized.champion_id] = initialized.model_copy(deep=True)
                return initialized.model_copy(deep=True)
            except Exception:
                connection.rollback()
                try:
                    self.record_champion_bootstrap_attempt(
                        bootstrap_id=bootstrap_id,
                        authorization_id=(authorization.authorization_id
                                          if authorization is not None else None),
                        authorization_consumed=authorization_consumed,
                        failure_reason="bootstrap_commit_failed",
                        failure_detail="Champion bootstrap authorization or registry transition failed.",
                    )
                except Exception:
                    pass
                raise

    def record_champion_bootstrap_attempt(
        self,
        *,
        bootstrap_id: str,
        authorization_id: Optional[str],
        authorization_consumed: bool,
        failure_reason: str,
        failure_detail: str,
    ) -> str:
        """Record a failed founding attempt without storing token material."""
        if not isinstance(authorization_consumed, bool):
            raise ValueError("authorization_consumed must be a boolean")
        attempt_id = str(uuid.uuid4())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO champion_bootstrap_attempts "
                "(attempt_id, bootstrap_id, authorization_id, authorization_consumed, "
                "failure_reason, failure_detail, attempted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, bootstrap_id, authorization_id, int(authorization_consumed),
                 str(failure_reason)[:100], str(failure_detail)[:2000],
                 datetime.now(timezone.utc).isoformat()),
            )
            connection.commit()
        return attempt_id

    def get_promotion_decision(self, decision_id: str):
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT decision_json, status, commit_result_json "
                "FROM promotion_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "decision": json.loads(row["decision_json"]),
            "status": row["status"],
            "commit_result": json.loads(row["commit_result_json"])
            if row["commit_result_json"] else None,
        }

    def is_challenge_committed(self, challenge_id: str) -> bool:
        """Returns whether a promotion challenge has already been committed."""
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM committed_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
        return row is not None

    def record_promotion_attempt(
        self,
        *,
        decision_id: str,
        challenge_id: str,
        champion_id: str,
        authorization_id: Optional[str],
        authorization_consumed: bool,
        failure_reason: str,
        failure_detail: str,
    ) -> str:
        """Durably record a blocked or failed promotion attempt without token contents."""
        if not isinstance(authorization_consumed, bool):
            raise ValueError("authorization_consumed must be a boolean")
        attempt_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO promotion_attempts "
                "(attempt_id, decision_id, challenge_id, champion_id, authorization_id, "
                "authorization_consumed, failure_reason, failure_detail, attempted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, decision_id, challenge_id, champion_id, authorization_id,
                 int(authorization_consumed), str(failure_reason)[:100],
                 str(failure_detail)[:2000], now),
            )
            connection.commit()
        return attempt_id

    def verify_state_consistency(self) -> Dict[str, Any]:
        """Cross-check champion rows and persisted governance evidence."""
        try:
            from Forge.core.promotion import CommitResult, PromotionDecision

            with closing(self._connect()) as connection:
                champion_rows = connection.execute(
                    "SELECT champion_id, revision, record_json FROM champions "
                    "ORDER BY champion_id"
                ).fetchall()
                decision_rows = connection.execute(
                    "SELECT decision_id, challenge_id, status, decision_json, "
                    "commit_result_json, committed_at FROM promotion_decisions "
                    "ORDER BY decision_id"
                ).fetchall()
                bootstrap_rows = connection.execute(
                    "SELECT bootstrap_id, champion_id, status, request_json, "
                    "result_json, committed_at FROM champion_bootstraps "
                    "ORDER BY bootstrap_id"
                ).fetchall()
                committed_challenges = {
                    row["challenge_id"] for row in connection.execute(
                        "SELECT challenge_id FROM committed_challenges"
                    ).fetchall()
                }

            for row in champion_rows:
                try:
                    champion = ChampionRecord.model_validate_json(row["record_json"])
                except Exception:
                    return {
                        "intact": False,
                        "issue": "champion_record_invalid",
                        "champion_id": row["champion_id"],
                    }
                tenure_ids = [item.tenure_id for item in champion.history]
                if champion.current_tenure is not None:
                    tenure_ids.append(champion.current_tenure.tenure_id)
                if (
                    champion.champion_id != row["champion_id"]
                    or champion.revision != row["revision"]
                    or len(tenure_ids) != len(set(tenure_ids))
                    or (
                        champion.current_tenure is not None
                        and champion.current_tenure in champion.history
                    )
                ):
                    return {
                        "intact": False,
                        "issue": "champion_record_index_or_history_mismatch",
                        "champion_id": row["champion_id"],
                    }

            for row in decision_rows:
                try:
                    decision_model = PromotionDecision.model_validate_json(
                        row["decision_json"]
                    )
                    decision = decision_model.model_dump(mode="json")
                except Exception:
                    return {
                        "intact": False,
                        "issue": "promotion_decision_invalid",
                        "decision_id": row["decision_id"],
                    }
                if (
                    decision.get("decision_id") != row["decision_id"]
                    or decision.get("challenge_id") != row["challenge_id"]
                    or row["status"] not in {"EVALUATED", "COMMITTED"}
                ):
                    return {
                        "intact": False,
                        "issue": "promotion_decision_index_mismatch",
                        "decision_id": row["decision_id"],
                    }
                if row["status"] == "EVALUATED":
                    if row["commit_result_json"] is not None or row["committed_at"] is not None:
                        return {
                            "intact": False,
                            "issue": "uncommitted_decision_has_commit_result",
                            "decision_id": row["decision_id"],
                        }
                else:
                    try:
                        result_model = CommitResult.model_validate_json(
                            row["commit_result_json"]
                        )
                        result = result_model.model_dump(mode="json")
                    except Exception:
                        return {
                            "intact": False,
                            "issue": "promotion_commit_result_invalid",
                            "decision_id": row["decision_id"],
                        }
                    if (
                        row["committed_at"] is None
                        or decision.get("outcome") != "APPROVED"
                        or result.get("success") is not True
                        or type(decision.get("expected_revision")) is not int
                        or result.get("challenge_id") != row["challenge_id"]
                        or result.get("champion_id") != decision.get("champion_id")
                        or result.get("registry_revision") != decision.get("expected_revision", -1) + 1
                        or row["challenge_id"] not in committed_challenges
                    ):
                        return {
                            "intact": False,
                            "issue": "promotion_commit_binding_mismatch",
                            "decision_id": row["decision_id"],
                        }

            for row in bootstrap_rows:
                try:
                    request = ChampionBootstrapRequest.model_validate_json(row["request_json"])
                except Exception:
                    return {
                        "intact": False,
                        "issue": "champion_bootstrap_request_invalid",
                        "bootstrap_id": row["bootstrap_id"],
                    }
                if (
                    request.bootstrap_id != row["bootstrap_id"]
                    or request.champion_id != row["champion_id"]
                    or row["status"] not in {"PREPARED", "COMMITTED"}
                ):
                    return {
                        "intact": False,
                        "issue": "champion_bootstrap_index_mismatch",
                        "bootstrap_id": row["bootstrap_id"],
                    }
                if row["status"] == "PREPARED":
                    if row["result_json"] is not None or row["committed_at"] is not None:
                        return {
                            "intact": False,
                            "issue": "prepared_bootstrap_has_commit_result",
                            "bootstrap_id": row["bootstrap_id"],
                        }
                else:
                    try:
                        result = ChampionRecord.model_validate_json(row["result_json"])
                    except Exception:
                        return {
                            "intact": False,
                            "issue": "champion_bootstrap_result_invalid",
                            "bootstrap_id": row["bootstrap_id"],
                        }
                    if (
                        row["committed_at"] is None
                        or result.champion_id != request.champion_id
                        or result.domain != request.domain
                        or result.context_key != request.context_key
                        or result.owner_agent != request.owner_agent
                        or result.revision != request.expected_revision + 1
                        or result.current_tenure is None
                        or result.current_tenure.candidate_id != request.candidate_id
                        or result.current_tenure.evidence_reference.get("bootstrap_id")
                        != request.bootstrap_id
                    ):
                        return {
                            "intact": False,
                            "issue": "champion_bootstrap_result_mismatch",
                            "bootstrap_id": row["bootstrap_id"],
                        }

            return {
                "intact": True,
                "champions": len(champion_rows),
                "promotion_decisions": len(decision_rows),
                "bootstraps": len(bootstrap_rows),
            }
        except Exception as exc:
            log.exception("champion registry consistency check failed")
            return {"intact": False, "issue": type(exc).__name__}

    def promotion_stats(self) -> Dict[str, Any]:
        """Report durable champion and promotion-decision state for operations."""
        with closing(self._connect()) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            champion_count = connection.execute(
                "SELECT COUNT(*) FROM champions"
            ).fetchone()[0]
            committed_count = connection.execute(
                "SELECT COUNT(*) FROM committed_challenges"
            ).fetchone()[0]
            decisions = connection.execute(
                "SELECT status, COUNT(*) AS count FROM promotion_decisions GROUP BY status"
            ).fetchall()
            bootstraps = connection.execute(
                "SELECT status, COUNT(*) AS count FROM champion_bootstraps GROUP BY status"
            ).fetchall()
            attempt_stats = connection.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(authorization_consumed), 0) AS consumed_authorization_failures "
                "FROM promotion_attempts"
            ).fetchone()
            last_attempt = connection.execute(
                "SELECT attempt_id, decision_id, authorization_id, failure_reason, "
                "failure_detail, authorization_consumed, attempted_at "
                "FROM promotion_attempts ORDER BY attempted_at DESC, attempt_id DESC LIMIT 1"
            ).fetchone()
            bootstrap_attempt_stats = connection.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(authorization_consumed), 0) AS consumed_authorization_failures "
                "FROM champion_bootstrap_attempts"
            ).fetchone()
            last_bootstrap_attempt = connection.execute(
                "SELECT attempt_id, bootstrap_id, authorization_id, failure_reason, "
                "failure_detail, authorization_consumed, attempted_at "
                "FROM champion_bootstrap_attempts "
                "ORDER BY attempted_at DESC, attempt_id DESC LIMIT 1"
            ).fetchone()
        state = self.verify_state_consistency()
        return {
            "database_integrity": integrity == "ok",
            "state_consistency": state,
            "champions": champion_count,
            "committed_challenges": committed_count,
            "promotion_decisions_by_status": {
                row["status"]: row["count"] for row in decisions
            },
            "champion_bootstraps_by_status": {
                row["status"]: row["count"] for row in bootstraps
            },
            "failed_promotion_attempts": attempt_stats["total"],
            "failed_attempts_after_authorization_consumption": (
                attempt_stats["consumed_authorization_failures"]
            ),
            "last_failed_promotion_attempt": dict(last_attempt) if last_attempt else None,
            "failed_champion_bootstrap_attempts": bootstrap_attempt_stats["total"],
            "bootstrap_failures_after_authorization_consumption": (
                bootstrap_attempt_stats["consumed_authorization_failures"]
            ),
            "last_failed_champion_bootstrap_attempt": (
                dict(last_bootstrap_attempt) if last_bootstrap_attempt else None
            ),
            "ready": integrity == "ok" and state.get("intact") is True,
        }
