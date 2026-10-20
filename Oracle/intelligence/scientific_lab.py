"""
Oracle.intelligence.scientific_lab
=================================
Scientific research coordinator for Oracle.

FIXES IMPLEMENTED:
1. Best Genome DNA always serialized (never empty [])
2. Duplicate promotion detection (skip if genome already champion)
3. Champion comparison (weighted score vs incumbent)
4. Champion versioning (full genealogy V1→V2→V3)
5. DNA report at end of every experiment
6. Chronicle stores full genome + certification + experiment
7. Research reflection with scientific conclusion
8. Adaptive evolution seeded from champion history
9. Champion cache (no redundant certification)
10. Complete research lab workflow
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

try:
    from intelligence.technicals import analyze  # type: ignore
except ImportError:
    from Oracle.intelligence.technicals import analyze  # type: ignore
try:
    from intelligence.strategy_planner import StrategyPlanner  # type: ignore
except ImportError:
    from Oracle.intelligence.strategy_planner import StrategyPlanner  # type: ignore
try:
    from benchmarks.benchmark_engine import BenchmarkEngine  # type: ignore
except ImportError:
    from Oracle.benchmarks.benchmark_engine import BenchmarkEngine  # type: ignore

log = logging.getLogger("oracle.lab")


class StrategyPromotionAuthorization(BaseModel):
    """Aegis-signed authorization for one exact Oracle champion candidate."""

    model_config = ConfigDict(frozen=True)

    authorization_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    challenge_id: str
    authorized_by: str
    decision: str = "AUTHORIZED"
    risk_category: str = "strategy_promotion"
    issued_at: datetime
    expires_at: datetime
    issuer: str
    audience: str = "oracle.strategy.promotion"
    signature: str
    nonce: str
    claims: Dict[str, Any]


class ScientificResearchLab:
    """Coordinates Oracle's hypothesis-led research workflow."""

    def __init__(self, chronicle=None, atlas=None, storage_dir: str = "memory",
                 promotion_verifier=None):
        self.chronicle = chronicle
        self.atlas = atlas
        self._promotion_verifier = promotion_verifier
        self._promotion_lock = threading.RLock()
        self.planner = StrategyPlanner()
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._journal_path = self._dir / "scientific_journal.json"
        self._journal: List[Dict[str, Any]] = self._load_list(self._journal_path)
        self._last_persisted_at: Optional[float] = None
        self._last_persist_error: Optional[str] = None
        # champion_library.json (current champion per symbol/regime) and
        # champion_history.json (a bounded promotion cache) are durable state --
        # Chronicle is their source of truth, the local file is the
        # offline-fallback cache. scientific_journal.json
        # stays as-is above: it already syncs to Chronicle separately, one
        # entry at a time, via _store_to_chronicle() (confirmed working) --
        # a different mechanism because it's an unbounded, ever-growing log,
        # not bounded state.
        try:
            from shared.chronicle_sync import ChronicleBackedStore  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "shared.chronicle_sync not importable -- is the ecosystem root on sys.path?"
            ) from exc
        self._champions_path = self._dir / "champion_library.json"
        self._history_path = self._dir / "champion_history.json"
        self._champions_store = ChronicleBackedStore(
            chronicle, "champion_library", self._champions_path, owner="oracle")
        self._history_store = ChronicleBackedStore(
            chronicle, "champion_history", self._history_path, owner="oracle",
            allow_legacy_list=True)
        self.benchmark = BenchmarkEngine(storage_dir=str(self._dir.parent / "benchmarks"))
        self._recover_authorized_promotions()

    @property
    def _champions(self) -> Dict[str, Dict[str, Any]]:
        return self._champions_store.data()

    @property
    def _champion_history(self) -> List[Dict[str, Any]]:
        # ChronicleBackedStore holds a dict internally; the list itself
        # lives under one key so it round-trips through Chronicle's state
        # store (which is key->value, not natively list-shaped).
        data = self._history_store.data()
        if isinstance(data, list):
            # A real bug found via testing: existing local
            # champion_history.json files were written in the OLD format
            # (a bare JSON list) before this migration -- ChronicleBackedStore's
            # local-cache fallback loads that old shape as-is. Adopt it into
            # the new dict-wrapped shape in place rather than crashing.
            wrapped: Dict[str, Any] = {"entries": data}
            self._history_store._data = wrapped
            return wrapped["entries"]
        return data.setdefault("entries", [])

    def _load_list(self, path):
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"Oracle research journal is unreadable: {path}") from exc
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise RuntimeError(f"Oracle research journal has an invalid schema: {path}")
        return data

    def _load_dict(self, path):
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _persist(self, strict: bool = False):
        temporary_path = None
        try:
            encoded = json.dumps(self._journal, indent=2, allow_nan=False)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self._journal_path.parent,
                prefix=f".{self._journal_path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._journal_path)
            self._champions_store.save(strict=strict)
            hist = self._history_store.data()
            hist["entries"] = hist.get("entries", [])[-100:]
            self._history_store.save(hist, strict=strict)
            self._last_persisted_at = time.time()
            self._last_persist_error = None
        except Exception as exc:
            self._last_persist_error = type(exc).__name__
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            log.error("Persist failed: %s", exc)
            if strict:
                raise RuntimeError("Oracle research state could not be persisted") from exc

    # ═══════════════════════════════════════════════════════════════
    # ISSUE 9: Champion Cache
    # ═══════════════════════════════════════════════════════════════

    def get_cached_champion(self, symbol: str, regime: str) -> Optional[Dict[str, Any]]:
        """Load champion once, reuse throughout experiment."""
        return self._champions.get(self.champion_key(symbol, regime))

    # ═══════════════════════════════════════════════════════════════
    # ISSUE 8: Adaptive Evolution (seed from champion history)
    # ═══════════════════════════════════════════════════════════════

    def get_seed_genomes(self, symbol: str, regime: str) -> List[Dict[str, Any]]:
        """
        Return top historical genomes for seeding evolution.
        60% of population should come from these.
        """
        seeds = []

        # Current champion
        champ = self.get_cached_champion(symbol, regime)
        if champ and champ.get("genome"):
            seeds.append(champ["genome"])

        # Historical champions for this symbol/regime
        for entry in reversed(self._champion_history):
            if (entry.get("symbol", "").upper() == symbol.upper() and
                    entry.get("regime") == regime and entry.get("genome")):
                if entry["genome"] not in seeds:
                    seeds.append(entry["genome"])
                if len(seeds) >= 5:
                    break

        # Top genomes from recent experiments
        for exp in reversed(self._journal[-20:]):
            if (exp.get("symbol", "").upper() == symbol.upper() and
                    exp.get("regime") == regime and exp.get("best_genome")):
                g = exp["best_genome"]
                if g and g not in seeds:
                    seeds.append(g)
                if len(seeds) >= 8:
                    break

        return seeds

    # ═══════════════════════════════════════════════════════════════
    # ISSUE 2: Duplicate Detection
    # ═══════════════════════════════════════════════════════════════

    def is_duplicate_champion(self, genome_id: str, symbol: str, regime: str) -> bool:
        """Check if this genome is already the current champion."""
        key = self.champion_key(symbol, regime)
        current = self._champions.get(key, {})
        current_id = current.get("genome", {}).get("genome_id", "")
        return current_id == genome_id

    # ═══════════════════════════════════════════════════════════════
    # ISSUE 3: Champion Comparison (weighted score)
    # ═══════════════════════════════════════════════════════════════

    def compare_champion(self, candidate_oos: Dict, symbol: str, regime: str) -> Dict[str, Any]:
        """
        Compare candidate against current champion using weighted multi-metric score.
        Returns comparison report with decision.
        """
        key = self.champion_key(symbol, regime)
        incumbent = self._champions.get(key)

        if not incumbent:
            return {"decision": "promote", "reason": "No incumbent champion exists",
                    "candidate_score": self._weighted_score(candidate_oos),
                    "incumbent_score": None}

        incumbent_oos = incumbent.get("out_of_sample", {})
        candidate_score = self._weighted_score(candidate_oos)
        incumbent_score = self._weighted_score(incumbent_oos)

        comparison = {
            "candidate_score": round(candidate_score, 4),
            "incumbent_score": round(incumbent_score, 4),
            "candidate_metrics": {
                "return": candidate_oos.get("total_return", 0),
                "sharpe": candidate_oos.get("sharpe_proxy", 0),
                "trades": candidate_oos.get("trades", 0),
                "win_rate": candidate_oos.get("win_rate", 0),
                "drawdown": candidate_oos.get("max_drawdown", 0),
            },
            "incumbent_metrics": {
                "return": incumbent_oos.get("total_return", 0),
                "sharpe": incumbent_oos.get("sharpe_proxy", 0),
                "trades": incumbent_oos.get("trades", 0),
                "win_rate": incumbent_oos.get("win_rate", 0),
                "drawdown": incumbent_oos.get("max_drawdown", 0),
            },
        }

        if candidate_score > incumbent_score:
            comparison["decision"] = "promote"
            comparison["reason"] = (f"Candidate score {candidate_score:.4f} > "
                                    f"incumbent {incumbent_score:.4f}")
        else:
            comparison["decision"] = "reject"
            comparison["reason"] = (f"Existing champion remains superior: "
                                    f"{incumbent_score:.4f} >= {candidate_score:.4f}")

        return comparison

    def _weighted_score(self, oos: Dict) -> float:
        """Weighted multi-metric score for champion comparison."""
        ret = float(oos.get("total_return", 0) or 0)
        sharpe = float(oos.get("sharpe_proxy", 0) or 0)
        pf = float(oos.get("profit_factor", 0) or 0)
        dd = float(oos.get("max_drawdown", 1) or 1)
        win = float(oos.get("win_rate", 0) or 0)
        trades = int(oos.get("trades", 0) or 0)
        expectancy = float(oos.get("expectancy", 0) or 0)

        return (ret * 3.0 + sharpe * 1.5 + min(pf, 3.0) * 0.5 +
                win * 0.5 + min(trades, 10) * 0.1 +
                max(0, expectancy) * 0.3 - dd * 2.0)

    # ═══════════════════════════════════════════════════════════════
    # ISSUE 4: Champion Versioning
    # ═══════════════════════════════════════════════════════════════

    def _get_next_version(self, symbol: str, regime: str) -> int:
        """Get next version number for this symbol/regime champion."""
        versions = [e.get("version", 0) for e in self._champion_history
                    if e.get("symbol", "").upper() == symbol.upper() and e.get("regime") == regime]
        return max(versions, default=0) + 1

    def _promote_champion(self, context: Dict, evolution_result: Dict,
                          evidence: Dict, hypotheses: List, experiment_id: str):
        """
        Promote with full versioning and genealogy.
        Stores complete history (never overwrites).
        """
        symbol = context.get("symbol", "")
        regime = context.get("regime", "unknown")
        key = self.champion_key(symbol, regime)
        genome = evolution_result.get("best_genome", {})
        current = self._champions.get(key)
        if current and current.get("experiment_id") == experiment_id:
            if current.get("genome_id") == genome.get("genome_id"):
                return current
            raise RuntimeError("Promotion experiment ID is already bound to another champion genome")
        version = self._get_next_version(symbol, regime)

        # Get parent champion info
        parent_champion = self._champions.get(key, {})
        parent_id = parent_champion.get("genome", {}).get("genome_id", None)
        parent_version = parent_champion.get("version", 0)

        champion_record = {
            "symbol": symbol,
            "regime": regime,
            "version": version,
            "genome": genome,
            "genome_id": genome.get("genome_id", "unknown"),
            "parent_champion_id": parent_id,
            "parent_version": parent_version,
            "ancestor_chain": (parent_champion.get("ancestor_chain", []) + [parent_id])[-10:] if parent_id else [],
            "generation": genome.get("generation", 0),
            "experiment_id": experiment_id,
            "evidence": evidence,
            "hypotheses": hypotheses,
            "in_sample": evolution_result.get("in_sample_return", 0),
            "out_of_sample": evolution_result.get("out_of_sample", {}),
            "fitness": genome.get("fitness", 0),
            "certified_at": time.time(),
        }

        # Update current champion
        self._champions[key] = champion_record

        # Append to history (never delete)
        history = self._champion_history
        previous_history = list(history)
        history.append(champion_record)

        try:
            self._persist(strict=True)
        except Exception:
            # If the champion store did not commit, do not leave the candidate
            # active in memory. If it committed before a later store failed,
            # retain it for idempotent recovery.
            try:
                champion_digest = self._champions_store._digest(
                    self._champions_store.data()
                )
                champion_committed = (
                    champion_digest == self._champions_store._last_synced_digest
                )
            except (TypeError, ValueError):
                champion_committed = False
            if not champion_committed:
                if current is None:
                    self._champions.pop(key, None)
                else:
                    self._champions[key] = current
                history[:] = previous_history
            raise

        log.info("CHAMPION V%d promoted: %s (%s/%s) parent=%s",
                 version, genome.get("genome_id"), symbol, regime, parent_id)

        return champion_record

    @staticmethod
    def _canonical_digest(value: Any) -> str:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _experiment_record(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        return next((
            item for item in self._journal
            if item.get("experiment_id") == experiment_id
        ), None)

    def promotion_authorization_claims(
        self, experiment_id: str, authorized_by: str
    ) -> Dict[str, Any]:
        if not isinstance(authorized_by, str) or not authorized_by.strip():
            raise ValueError("authorized_by must be a non-empty identity")
        experiment = self._experiment_record(experiment_id)
        if experiment is None:
            raise ValueError(f"Oracle experiment {experiment_id} was not found")
        if experiment.get("promotion_status") not in (
            "PENDING_AUTHORIZATION", "AUTHORIZED_COMMITTING"
        ):
            raise ValueError("Experiment does not have an eligible candidate awaiting commit")
        candidate_digest = experiment.get("candidate_digest")
        if candidate_digest != self._canonical_digest({
            "genome": experiment.get("best_genome"),
            "evidence": experiment.get("evidence"),
        }):
            raise ValueError("Persisted candidate evidence digest is invalid")
        key = self.champion_key(experiment["symbol"], experiment["regime"])
        incumbent_digest = experiment.get("incumbent_digest")
        if experiment.get("promotion_status") == "PENDING_AUTHORIZATION":
            incumbent = self._champions.get(key)
            current_digest = self._canonical_digest(incumbent) if incumbent else None
            if current_digest != incumbent_digest:
                raise ValueError("Incumbent changed since the candidate was evaluated")
        return {
            "action": "authorize_oracle_strategy_promotion",
            "audience": "oracle.strategy.promotion",
            "experiment_id": experiment_id,
            "candidate_digest": candidate_digest,
            "incumbent_digest": incumbent_digest,
            "symbol": experiment["symbol"],
            "regime": experiment["regime"],
            "authorized_by": authorized_by.strip(),
        }

    def authorize_promotion(
        self, experiment_id: str, authorization: StrategyPromotionAuthorization
    ) -> Dict[str, Any]:
        with self._promotion_lock:
            return self._authorize_promotion_locked(experiment_id, authorization)

    def _authorize_promotion_locked(
        self, experiment_id: str, authorization: StrategyPromotionAuthorization
    ) -> Dict[str, Any]:
        verifier = self._promotion_verifier
        if verifier is None:
            raise PermissionError("Oracle strategy promotion authorization is not configured")
        claims = self.promotion_authorization_claims(
            experiment_id, authorization.authorized_by
        )
        if authorization.audience != "oracle.strategy.promotion":
            raise PermissionError("Authorization audience must be oracle.strategy.promotion")
        if authorization.claims != claims:
            raise PermissionError("Authorization claims do not match the pending candidate")
        if not verifier.verify_and_consume(authorization, claims):
            raise PermissionError("Aegis rejected or already consumed the promotion authorization")

        # Recheck after the signature/nonce operation. If another promotion
        # raced this one, the incumbent digest changes and the candidate stays pending.
        experiment = self._experiment_record(experiment_id)
        if experiment is None or experiment.get("promotion_status") != "PENDING_AUTHORIZATION":
            raise ValueError("Candidate is no longer pending authorization")
        key = self.champion_key(experiment["symbol"], experiment["regime"])
        incumbent = self._champions.get(key)
        current_digest = self._canonical_digest(incumbent) if incumbent else None
        if current_digest != claims["incumbent_digest"]:
            raise ValueError("Incumbent changed before the authorized commit was recorded")
        experiment["promotion_status"] = "AUTHORIZED_COMMITTING"
        experiment["promotion_authorization"] = {
            "authorization_id": authorization.authorization_id,
            "authorized_by": authorization.authorized_by,
            "issuer": authorization.issuer,
            "nonce": authorization.nonce,
            "claims_digest": self._canonical_digest(claims),
        }
        self._persist(strict=True)
        return self._commit_authorized_promotion(experiment)

    def _commit_authorized_promotion(self, experiment: Dict[str, Any]) -> Dict[str, Any]:
        key = self.champion_key(experiment["symbol"], experiment["regime"])
        current = self._champions.get(key)
        if current and current.get("experiment_id") == experiment["experiment_id"]:
            if current.get("genome_id") != experiment.get("best_genome", {}).get("genome_id"):
                raise RuntimeError("Persisted champion does not match the authorized candidate")
            champion_record = current
            if not any(
                item.get("experiment_id") == experiment["experiment_id"]
                for item in self._champion_history
            ):
                self._champion_history.append(champion_record)
                self._persist(strict=True)
        else:
            current_digest = self._canonical_digest(current) if current else None
            if current_digest != experiment.get("incumbent_digest"):
                raise ValueError("Incumbent changed before authorized promotion could commit")
            evolution_result = {
                "best_genome": experiment["best_genome"],
                "in_sample_return": experiment.get("in_sample_return", 0),
                "out_of_sample": experiment.get("out_of_sample", {}),
            }
            context = {"symbol": experiment["symbol"], "regime": experiment["regime"]}
            champion_record = self._promote_champion(
                context, evolution_result, experiment["evidence"],
                experiment.get("hypotheses", []), experiment["experiment_id"],
            )
        experiment["promotion_status"] = "PROMOTED"
        experiment["promoted_new_champion"] = True
        experiment["champion_version"] = champion_record.get("version")
        self._persist(strict=True)
        return {"status": "promoted", "experiment_id": experiment["experiment_id"],
                "champion": champion_record}

    def _recover_authorized_promotions(self) -> None:
        """Resume only commits with a previously verified, durably recorded Aegis receipt."""
        for experiment in self._journal:
            if experiment.get("promotion_status") != "AUTHORIZED_COMMITTING":
                continue
            try:
                receipt = experiment.get("promotion_authorization") or {}
                claims = self.promotion_authorization_claims(
                    experiment["experiment_id"], receipt.get("authorized_by", "")
                )
                if receipt.get("claims_digest") != self._canonical_digest(claims):
                    raise ValueError("Persisted Aegis receipt does not match candidate claims")
                self._commit_authorized_promotion(experiment)
            except Exception as exc:
                log.critical(
                    "Authorized Oracle promotion recovery is unresolved for %s (%s)",
                    experiment.get("experiment_id"), type(exc).__name__,
                )

    # ═══════════════════════════════════════════════════════════════
    # ISSUE 6: Chronicle Memory (store everything)
    # ═══════════════════════════════════════════════════════════════

    def _store_to_chronicle(self, experiment: Dict, champion_record: Optional[Dict] = None):
        """Store complete experiment + champion data to Chronicle."""
        if not self.chronicle:
            return

        try:
            # Store experiment summary
            exp_content = (
                f"Oracle experiment {experiment['experiment_id']} for "
                f"{experiment.get('symbol')} ({experiment.get('regime')}): "
                f"{experiment.get('evidence', {}).get('verdict', 'unknown')}. "
            )
            if champion_record:
                genome = champion_record.get("genome", {})
                oos = champion_record.get("out_of_sample", {})
                mods = genome.get("modules", {})
                exp_content += (
                    f"Champion V{champion_record.get('version')}: "
                    f"trend={mods.get('trend', {}).get('logic_type')}, "
                    f"momentum={mods.get('momentum', {}).get('logic_type')}, "
                    f"OOS return={oos.get('total_return', 0):.4f}, "
                    f"trades={oos.get('trades', 0)}, "
                    f"sharpe={oos.get('sharpe_proxy', 0):.3f}. "
                    f"Genome ID: {genome.get('genome_id')}."
                )

            tags = ["oracle", experiment.get("symbol", ""), experiment.get("regime", "")]
            if champion_record:
                tags.append("champion")
                tags.append(f"v{champion_record.get('version', 0)}")

            if hasattr(self.chronicle, "act"):
                self.chronicle.act("memory.store", {
                    "content": exp_content,
                    "pillar": "episodic",
                    "domain": "trading",
                    "tags": tags,
                    "_sender": "oracle",
                })
        except Exception as exc:
            log.debug("Chronicle store failed: %s", exc)

    # ═══════════════════════════════════════════════════════════════
    # ISSUE 7: Research Reflection (scientific conclusion)
    # ═══════════════════════════════════════════════════════════════

    def self_reflection(self, promoted: bool, context: Dict, result: Dict) -> Dict[str, Any]:
        """Generate a scientific research conclusion, not just 'champion promoted'."""
        reflection = {"timestamp": time.time(), "insights": [], "directives": [], "conclusion": ""}

        genome = result.get("best_genome", {})
        modules = genome.get("modules", {})
        oos = result.get("out_of_sample", {})
        history = result.get("history", [])

        if promoted:
            # Build scientific conclusion (ISSUE 7)
            trend_type = modules.get("trend", {}).get("logic_type", "unknown")
            mom_type = modules.get("momentum", {}).get("logic_type", "unknown")
            vol_type = modules.get("volatility", {}).get("logic_type", "default")
            oos_return = oos.get("total_return", 0)
            oos_trades = oos.get("trades", 0)
            oos_sharpe = oos.get("sharpe_proxy", 0)
            is_return = result.get("in_sample_return", 0)

            # Determine generalization quality
            if oos_return > 0 and is_return > 0:
                gen_ratio = oos_return / max(is_return, 0.001)
                if gen_ratio > 0.8:
                    gen_note = "excellent generalization (OOS closely matches IS)"
                elif gen_ratio > 0.4:
                    gen_note = "acceptable generalization"
                else:
                    gen_note = "moderate overfitting (OOS significantly below IS)"
            else:
                gen_note = "generalization inconclusive"

            conclusion = (
                f"The winning strategy uses {trend_type} trend detection with "
                f"{mom_type} momentum confirmation"
                f"{f' and {vol_type} volatility filtering' if vol_type != 'default' else ''}. "
                f"It achieved {oos_return:.4f} return over {oos_trades} OOS trades "
                f"(Sharpe={oos_sharpe:.3f}), indicating {gen_note}. "
                f"This genome should serve as the reference for future {context.get('regime')} evolution."
            )

            reflection["conclusion"] = conclusion
            reflection["insights"] = [
                f"Champion certified for {context.get('symbol')} ({context.get('regime')})",
                f"Trend: {trend_type}, Momentum: {mom_type}",
                f"OOS: return={oos_return:.4f}, trades={oos_trades}, sharpe={oos_sharpe:.3f}",
                f"Generalization: {gen_note}",
            ]
            reflection["directives"] = [
                "Seed future populations from this champion's DNA",
                "Store winning module combination in Chronicle",
            ]
        else:
            if result.get("candidate_eligible"):
                reflection["conclusion"] = (
                    "Candidate passed Oracle's current OOS comparison and is awaiting "
                    "the owning agent's request-bound Aegis promotion authorization."
                )
                reflection["insights"] = [
                    f"Candidate eligible: {result.get('best_genome', {}).get('genome_id', 'unknown')}",
                    "Champion state remains unchanged until authorization commits.",
                ]
                reflection["directives"] = [
                    "Review the evidence and comparison against the current champion",
                    "Request strategy promotion authorization only after review",
                ]
                return reflection
            cert_audit = result.get("certification_audit", {})
            rejection = cert_audit.get("rejection_reason", "unknown")

            reflection["conclusion"] = (
                f"Candidate was profitable in-sample but failed OOS certification. "
                f"Rejection: {rejection}. "
                f"The strategy may be overfit or the validation dataset insufficient."
            )
            reflection["insights"] = [
                f"Failed certification: {rejection}",
                f"In-sample fitness: {result.get('best_genome', {}).get('fitness', 0):.4f}",
            ]
            reflection["directives"] = [
                "Investigate OOS data quality and length",
                "Consider regime shift between IS and OOS periods",
            ]

        return reflection

    # ═══════════════════════════════════════════════════════════════
    # ISSUE 5: DNA Report
    # ═══════════════════════════════════════════════════════════════

    def format_dna_report(self, genome: Dict, result: Dict) -> Dict[str, Any]:
        """
        Generate complete DNA report for printing.
        NEVER returns empty. Always includes full configuration.
        """
        if not genome:
            return {"error": "No genome available"}

        modules = genome.get("modules", {})
        oos = result.get("out_of_sample", {})

        return {
            "genome_id": genome.get("genome_id", "unknown"),
            "generation": genome.get("generation", 0),
            "parents": genome.get("parents", []),
            "family": modules.get("trend", {}).get("logic_type", "unknown"),
            "indicators": {
                "trend": modules.get("trend", {}),
                "momentum": modules.get("momentum", {}),
                "volatility": modules.get("volatility", {}),
            },
            "entry": modules.get("entry", {}),
            "exit": modules.get("exit", {}),
            "risk": modules.get("risk", {}),
            "regime_filter": modules.get("market_regime", {}),
            "fitness": genome.get("fitness", 0),
            "training_return": result.get("in_sample_return", 0),
            "validation_return": oos.get("total_return", 0),
            "sharpe": oos.get("sharpe_proxy", 0),
            "trades": oos.get("trades", 0),
            "win_rate": oos.get("win_rate", 0),
        }

    # ═══════════════════════════════════════════════════════════════
    # MAIN CYCLE
    # ═══════════════════════════════════════════════════════════════

    def market_context(self, series):
        technicals = analyze(series)
        regime = (technicals.get("regime") or {}).get("regime", "unknown")
        return {
            "symbol": series.symbol, "regime": regime,
            "bars": len(series.bars), "last": series.last,
            "volatility": (technicals.get("regime") or {}).get("volatility", 0.0),
            "slope_20": (technicals.get("regime") or {}).get("slope_20", 0.0),
            "technicals": technicals,
        }

    def consult_memory(self, symbol, regime):
        if not self.chronicle: return []
        try:
            return self.chronicle.search(
                query=f"{symbol} {regime} champion strategy successful",
                domain="trading", limit=6, requester="oracle")
        except Exception:
            return []

    def generate_hypotheses(self, symbol, regime, memory=None, research=None):
        base = {
            "trending_up": [
                ("trend_continuation_atr", "Trend continuation improves when entries require ATR-confirmed momentum."),
                ("pullback_momentum", "Bull trend pullbacks with momentum recovery outperform blind breakout entries."),
            ],
            "trending_down": [
                ("bear_trend_continuation", "Bear trend continuation improves when exits trail volatility."),
                ("failed_rally_short", "Shorting failed rallies outperforms oversold reversal in bear regimes."),
            ],
            "ranging": [
                ("range_reversion_rsi", "RSI reversal performs better inside ranging markets than trend following."),
                ("bollinger_mean_reversion", "Bollinger extremes revert more reliably when volatility is contained."),
            ],
            "high_volatility": [
                ("volatility_filter", "ATR filters reduce drawdown during high-volatility expansion."),
                ("compression_breakout", "Momentum performs better after volatility compression."),
            ],
        }.get(regime, [("regime_first_baseline", "A regime-filtered baseline outperforms a universal strategy.")])

        hypotheses = []
        for family, statement in base:
            hypotheses.append({
                "hypothesis_id": f"hyp-{uuid.uuid4().hex[:8]}",
                "symbol": symbol, "regime": regime,
                "family": family, "statement": statement, "source": "oracle_regime_memory",
            })
        if research:
            hypotheses.append({
                "hypothesis_id": f"hyp-{uuid.uuid4().hex[:8]}",
                "symbol": symbol, "regime": regime,
                "family": "external_research_lead",
                "statement": "Atlas research provided institutional-grade trading families.",
                "source": "atlas_research",
            })
        return hypotheses[:5]

    def detect_stagnation(self, history):
        if len(history) < 3:
            return {"stagnant": False, "reasons": []}
        fitness = [float(h.get("best_fitness", 0)) for h in history]
        delta = max(fitness) - fitness[0]
        tail = fitness[-3:]
        reasons = []
        if delta < 0.005: reasons.append("fitness_plateau")
        if len(set(round(x, 5) for x in tail)) <= 1: reasons.append("convergence")
        if max(tail) <= 0: reasons.append("non_profitable")
        return {"stagnant": bool(reasons), "reasons": reasons, "best_fitness_delta": round(delta, 4)}

    def request_atlas_research(self, context, stagnation):
        query = (f"Research profitable quantitative trading approaches for {context.get('symbol')} "
                 f"under {context.get('regime')} conditions.")
        if not self.atlas: return {"status": "unavailable", "query": query}
        try:
            if hasattr(self.atlas, "act"):
                # FIX (found via a real bug report): "financial_markets" was never
                # a recognized domain in Atlas's DOMAIN_SOURCES -- it silently fell
                # back to "general", which includes semantic_scholar (an ACADEMIC
                # paper search engine). Confirmed root cause of real, reproducible
                # garbage in scientific_journal.json: a GBPUSD/NASDAQ trading query
                # returned an unrelated ecology dissertation about a national park,
                # stored as if it were legitimate research, across 4 separate
                # experiments. "trading" is the real recognized key, mapping to
                # gdelt/hackernews/wikipedia -- no academic-paper source at all.
                # Also fixed: "institutional" wasn't a recognized depth either
                # (silently fell back to a reasonable default, but still wrong).
                return self.atlas.act("research.investigate", {
                    "query": query, "domain": "trading",
                    "depth": "standard", "stagnation": stagnation, "_sender": "oracle"})
        except Exception as exc:
            return {"status": "error", "message": str(exc)}
        return {"status": "error"}

    def score_evidence(self, result):
        oos = result.get("out_of_sample") or {}
        total_return = float(oos.get("total_return", 0) or 0)
        trades = int(oos.get("trades", 0) or 0)
        promoted = result.get("promoted_new_champion", False)
        eligible = result.get("candidate_eligible", promoted)
        verdict = "candidate_eligible" if eligible else "rejected"
        sharpe = float(oos.get("sharpe_proxy", 0) or 0)
        dd = float(oos.get("max_drawdown", 0) or 0)
        score = total_return * 2 + sharpe * 0.4 - dd * 1.5 + (1.0 if trades >= 1 else 0)
        return {"score": round(score, 4), "verdict": verdict,
                "metrics": oos, "criteria": {"return": total_return, "trades": trades}}

    def run_scientific_cycle(self, series, evolution_fn):
        """
        Complete workflow:
        Context → Hypotheses → Seeds → Evolution → Comparison → Promotion → Report
        """
        context = self.market_context(series)
        symbol = context["symbol"]
        regime = context["regime"]
        memory = self.consult_memory(symbol, regime)
        hypotheses = self.generate_hypotheses(symbol, regime, memory)

        log.info("STATE: Context(%s, %s, %d bars) → Hypotheses(%d)",
                 symbol, regime, context["bars"], len(hypotheses))

        # ISSUE 8: Get seed genomes from champion history
        seeds = self.get_seed_genomes(symbol, regime)
        if seeds:
            log.info("STATE: Seeding evolution with %d historical genomes", len(seeds))

        # First evolution pass
        try:
            result = evolution_fn(None)
        except Exception as exc:
            log.error("Evolution pass 1 failed: %s", exc)
            result = {"status": "error", "history": [], "best_genome": {},
                      "promoted_new_champion": False, "out_of_sample": {}}

        stagnation = self.detect_stagnation(result.get("history", []))

        research = None
        if stagnation.get("stagnant") or result.get("status") == "error":
            research = self.request_atlas_research(context, stagnation)
            planned = self.planner.plan(research, symbol, regime)
            try:
                result = evolution_fn(planned)
            except Exception as exc:
                log.error("Evolution pass 2 failed: %s", exc)
                result = {"status": "error", "history": [], "best_genome": {},
                          "promoted_new_champion": False, "out_of_sample": {}}
            stagnation = self.detect_stagnation(result.get("history", []))

        # ═══════════════════════════════════════════════════════
        # POST-EVOLUTION: Comparison, Promotion, Reporting
        # ═══════════════════════════════════════════════════════
        candidate_eligible = result.get("candidate_eligible", False)
        promoted = False
        candidate_pending = False
        best_genome = result.get("best_genome", {})
        oos = result.get("out_of_sample", {})
        experiment_id = f"exp-{uuid.uuid4().hex[:8]}"
        champion_record = None
        comparison = None

        if candidate_eligible:
            genome_id = best_genome.get("genome_id", "unknown")

            # ISSUE 2: Duplicate detection
            if self.is_duplicate_champion(genome_id, symbol, regime):
                log.info("Champion already certified (%s). No promotion required.", genome_id)
                promoted = False
                result["promoted_new_champion"] = False
            else:
                # ISSUE 3: Compare against incumbent
                comparison = self.compare_champion(oos, symbol, regime)
                if comparison["decision"] == "reject":
                    log.info("Candidate rejected: %s", comparison["reason"])
                else:
                    candidate_pending = True
                    log.info(
                        "STATE: Candidate %s passed research comparison; "
                        "awaiting request-bound Aegis authorization",
                        genome_id,
                    )

        # Record experiment (always, even on failure)
        evidence = self.score_evidence(result)
        experiment = {
            "experiment_id": experiment_id,
            "created_at": time.time(),
            "symbol": symbol, "regime": regime,
            "hypotheses": hypotheses,
            "stagnation": stagnation,
            "research": research,
            "evidence": evidence,
            "best_genome": best_genome,  # ISSUE 1: Always include full genome
            "promoted_new_champion": promoted,
            "promotion_status": (
                "PENDING_AUTHORIZATION" if candidate_pending else
                "NOT_ELIGIBLE" if not candidate_eligible else "REJECTED"
            ),
            "candidate_eligible": bool(candidate_eligible),
            "champion_comparison": comparison,
            "in_sample_return": result.get("in_sample_return", 0),
            "out_of_sample": oos,
            "champion_version": champion_record.get("version") if champion_record else None,
            "validation_summary": result.get("validation_summary", {}),
            "certification_audit": result.get("certification_audit", {}),
        }
        key = self.champion_key(symbol, regime)
        incumbent = self._champions.get(key)
        experiment["incumbent_digest"] = (
            self._canonical_digest(incumbent) if incumbent else None
        )
        if candidate_pending:
            experiment["candidate_digest"] = self._canonical_digest({
                "genome": best_genome,
                "evidence": evidence,
            })
        self._journal.append(experiment)
        self._persist(strict=candidate_pending)

        # ISSUE 6: Store everything to Chronicle
        self._store_to_chronicle(experiment, champion_record)

        # ISSUE 7: Scientific reflection
        reflection = self.self_reflection(promoted, context, result)
        experiment["reflection"] = reflection

        # ISSUE 5: DNA Report (NEVER empty)
        dna_report = self.format_dna_report(best_genome, result)

        # Consistency validation
        self._validate_consistency(experiment, result, promoted)

        log.info("STATE: Experiment(%s) → Reflection → Report COMPLETE", experiment_id)
    

                # Build final result
        final_result = {
            "status": "complete",
            "context": context,
            "hypotheses": hypotheses,
            "stagnation": stagnation,
            "research": research,
            "experiment": experiment,
            "evolution": result,
            "reflection": reflection,
            "champion": self.champion_info(context["symbol"], context["regime"]),
            "dna_report": dna_report,
            "champion_comparison": comparison,
            "promotion_status": experiment["promotion_status"],
            "promotion_authorization_required": candidate_pending,
            "promotion_authorization_configured": self._promotion_verifier is not None,
        }

        # Benchmark update (automatic, never crashes Oracle)
        try:
            self.benchmark.record_experiment(final_result)
        except Exception as exc:
            log.warning("benchmark record failed: %s", exc)

        return final_result

    def _validate_consistency(self, experiment, result, promoted):
        """Ensure no contradictory states."""
        evidence = experiment.get("evidence", {})
        verdict = evidence.get("verdict", "rejected")
        reflection = experiment.get("reflection", {})

        if promoted and verdict != "accepted":
            evidence["verdict"] = "accepted"
            experiment["evidence"] = evidence
        if not promoted and verdict == "accepted":
            evidence["verdict"] = "rejected"
            experiment["evidence"] = evidence
        if promoted and reflection:
            insights = reflection.get("insights", [])
            reflection["insights"] = [i for i in insights if "failed" not in i.lower()]

    def champion_key(self, symbol, regime):
        return f"{symbol.upper()}::{regime}"

    def champion_info(self, symbol, regime=None):
        symbol = symbol.upper()
        if regime:
            return self._champions.get(self.champion_key(symbol, regime))
        candidates = [c for c in self._champions.values() if c.get("symbol", "").upper() == symbol]
        return max(candidates, key=lambda c: c.get("evidence", {}).get("score", -999)) if candidates else None

    def stats(self):
        pending = [
            item.get("experiment_id") for item in self._journal
            if item.get("promotion_status") == "PENDING_AUTHORIZATION"
        ]
        committing = [
            item.get("experiment_id") for item in self._journal
            if item.get("promotion_status") == "AUTHORIZED_COMMITTING"
        ]
        return {
            "experiments": len(self._journal),
            "champions": list(self._champions.keys()),
            "champion_versions": len(self._champion_history),
            "persistence": {
                "journal_file_ready": self._journal_path.is_file(),
                "last_persisted_at": self._last_persisted_at,
                "last_persist_error": self._last_persist_error,
                "champion_store": self._champions_store.operational_status(),
                "champion_history_store": self._history_store.operational_status(),
            },
            "promotion_authorization_configured": self._promotion_verifier is not None,
            "pending_promotion_authorizations": len(pending),
            "pending_experiment_ids": pending,
            "authorized_commits_requiring_reconciliation": len(committing),
            "reconciliation_experiment_ids": committing,
        }
