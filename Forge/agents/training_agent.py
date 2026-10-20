"""
Forge.agents.training_agent
=========================
Forge (formerly Training Engine): an ML training and evidence platform on the
constitutional BaseAgent. (Book I Article IX + XIII; Book III Part II Ch VIII.)

Forge can research and propose methods through Atlas. `training.evolve` is
proposal-only: it does not alter a preferred method or champion. Governed V2
challenge execution and promotion now have explicit agent tasks. Evolution
still requires an initialized V2 incumbent, and promotion remains fail-closed
until trusted Aegis and deployment health adapters are injected. Methods
requiring unavailable libraries are reported as recommendations, never
fabricated.

Training jobs validate data, select a backend, tune, train, evaluate, register
an experimental artifact, and capture a drift baseline. Deployment requires
the separate governed staged-deployment path.
"""
from __future__ import annotations

import logging
import hashlib
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_ECO_ROOT = Path(__file__).resolve().parents[2]
if str(_ECO_ROOT) not in sys.path:
    sys.path.insert(0, str(_ECO_ROOT))

from core.dataset import (DatasetLoader, Dataset, DataValidator, train_test_indices,  # type: ignore
                          kfold_indices, standardize)
from core.backends import (all_backends, available_backends, backend_catalog,        # type: ignore
                           UnifiedModel, evaluate_supervised)
from core.extra_backends import discovery_backends, predict_with                     # type: ignore
from core.model_registry import ModelRegistry                                        # type: ignore
from Forge.core.adapters.discovery_translator import DiscoveryProfile, DiscoveryTranslator
from Forge.core.adapters.ml_adapter import MLExperimentAdapter
from Forge.core.adapters.prompt_adapter import PromptOptimizationAdapter
from Forge.core.adapters.workflow_adapter import WorkflowOptimizationAdapter
from Forge.core.adapters.config_adapter import ConfigOptimizationAdapter
from Forge.core.benchmark import Benchmark, BenchmarkContext, BenchmarkReport
from Forge.core.challenger import (
    ChallengeReconciliationAuthorization,
    ChallengeRequest,
    ChampionRegistry,
)
from Forge.core.challenger_engine import ChallengerEngine
from Forge.core.evaluator import Evaluator
from Forge.core.experimentation import (
    ExperimentContract,
    ExperimentStatus,
    ExperimentDomain,
    ExperimentRunner,
    RunReconciliationAuthorization,
)
from Forge.core.knowledge_outbox import KnowledgeOutbox
from Forge.core.drift import DriftMonitor, DriftPolicy, DriftPolicyResolver
from Forge.core.drift_orchestrator import DriftOrchestrator
from Forge.core.metrics import PAIRED_BOOTSTRAP_METHODS, bootstrap_metric_samples
from Forge.core.promotion import (
    AegisRequirementConfig,
    AuthorizationToken,
    DefaultPromotionPolicy,
    PromotionDecision,
    PromotionGate,
)
from Forge.deployment.controller import (
    DeploymentAuthorization,
    DeploymentController,
)
from Forge.core.request_handler import RequestHandler, _TARGET_DOMAIN_MAP
from Forge.core.resource_scheduler import ResourceScheduler, SchedulerConfig
from Forge.core.reuse_registry import ReuseAuthorization, ReuseRegistry
from Forge.core.training_request import (
    RequestReconciliationAuthorization,
    TargetType,
    TrainingReport,
    TrainingRequest,
)
from intelligence.optimizer import BackendSelector, HyperparameterOptimizer          # type: ignore
from intelligence.method_discovery import MethodDiscovery                            # type: ignore
# Evidence Engine -- Forge's ecosystem-wide role (Hypothesis Queue +
# Experiment Templates), not just ML training. Bare import matches this
# file's existing convention; NOTE this file's own "intelligence"/"core"
# imports carry the same cross-repo module-collision risk found and fixed
# elsewhere this session if Forge is ever loaded as a peer AFTER another
# repo with its own "intelligence" package -- untested here, worth
# revisiting with the dual-import fallback pattern if that scenario comes up.
from intelligence.hypothesis_queue import HypothesisQueue                            # type: ignore
from intelligence.experiment_templates import run_template                          # type: ignore

try:
    from shared.agent import BaseAgent
    _HAS_SHARED = True
except Exception:
    _HAS_SHARED = False
    class BaseAgent:
        reasoning = None
        def __init__(self, **kw): self._started = False; self._handled = 0; self._failed = 0; self.llm = None
        def act(self, task, context=None): return self.execute(task, context or {})
        def get_status(self): return {"name": getattr(self, "name", "forge")}
        def solve(self, *a, **k): return {"status": "error", "message": "no reasoning"}
        has_brain = False
        def on_start(self): ...
        def start(self): self._started = True; self.on_start()
        def stop(self): self._started = False

log = logging.getLogger("forge")


class ForgeAgent(BaseAgent):
    name = "forge"
    repository = "Forge"
    domain = "training"
    description = "ML training and evidence platform with proposal-only method discovery and governed deployment controls."
    capabilities = ["training.run", "training.from_csv", "training.evolve", "data.validate",
                    "method.discover", "benchmark.execute", "model.register", "model.promote",
                    "model.leaderboard", "model.predict", "model.predict_active", "model.drift",
                    "deployment.prepare_rollout", "deployment.start_rollout",
                    "deployment.prepare_advance", "deployment.advance",
                    "deployment.prepare_rollback", "deployment.rollback", "deployment.status",
                    "evolution.discover", "evolution.challenge.run",
                    "evolution.promotion.evaluate", "evolution.promotion.authorization_request",
                    "evolution.promotion.commit", "evolution.status",
                    "evolution.challenge.reconciliation.authorization_request",
                    "evolution.challenge.reconciliation.reconcile",
                    "evolution.challenge.resume.authorization_request",
                    "evolution.challenge.resume",
                    "evolution.bootstrap.prepare", "evolution.bootstrap.authorization_request",
                    "evolution.bootstrap.commit",
                    "drift.assess", "drift.investigation.authorization_request",
                    "drift.investigation.execute", "drift.investigation.complete",
                    "hyperparameter.optimize", "backends.catalog",
                    "experiment.run", "experiment.contract.submit",
                    "experiment.contract.execute", "experiment.contract.evaluate",
                    "experiment.contract.report", "experiment.reconciliation.authorization_request",
                    "experiment.reconciliation.reconcile", "oracle.improvement.request",
                    "hypothesis.add", "hypothesis.list",
                    # Universal Training Institution (§26)
                    "training.request.submit", "training.request.status",
                    "training.request.authorization_request", "training.request.authorize",
                    "training.request.reconciliation.authorization_request",
                    "training.request.reconciliation.reconcile",
                    "training.request.report", "training.request.cancel",
                    "training.request.list", "training.request.scheduler_status",
                    "training.request.execute_next",
                    "training.reuse.search", "training.reuse.check",
                    "training.reuse.authorization_request", "training.reuse.approve",
                    "knowledge.outbox.drain",
                    "training.reuse.register"]
    channels = ["ecosystem.training", "ecosystem.optimization", "ecosystem.broadcast"]
    memory_namespace = "forge_memory"
    security_level = "elevated"
    mission = {"purpose": "Train and evaluate with rigor; change deployed models only through governed evidence and authorization."}

    def __init__(self, chronicle_client=None, atlas_client=None, genesis_client=None,
                 storage_dir=None, deployment_controller=None,
                 promotion_verifier=None, deployment_verifier=None,
                 deployment_health_gate=None, experiment_runner=None,
                 experiment_adapters=None,
                 experiment_max_concurrent_runs=2,
                 experiment_max_total_concurrent_runs=2,
                 experiment_reconciliation_verifier=None,
                 champion_registry=None, training_request_verifier=None,
                 reuse_compatibility_policy=None, reuse_authorization_verifier=None,
                 domain_owners=None,
                 **kw):
        super().__init__(chronicle_client=chronicle_client, atlas_client=atlas_client,
                        storage_dir=str(_REPO_ROOT / "memory"), **kw)
        self.loader = DatasetLoader()
        self.validator = DataValidator()
        self.registry = ModelRegistry(storage_dir=storage_dir or str(_REPO_ROOT / "models"))
        self.knowledge_outbox = KnowledgeOutbox(self.registry.database_path)
        self.deployment_controller = deployment_controller or DeploymentController(
            self.registry,
            authorization_verifier=deployment_verifier,
            health_gate=deployment_health_gate,
        )
        self.selector = BackendSelector(atlas_client=atlas_client)
        self.optimizer = HyperparameterOptimizer()
        self.discovery = MethodDiscovery(atlas=atlas_client, chronicle=chronicle_client,
                                        genesis=genesis_client, storage_dir=str(_REPO_ROOT / "memory"))
        # core backends + discoverable extra backends, unified
        self._backends = {b.name: b for b in all_backends()}
        self._backends.update(discovery_backends())
        # Evidence Engine (Hypothesis Queue + Experiment Templates) --
        # Forge's ecosystem-wide role, not just ML training.
        self.hypothesis_queue = HypothesisQueue()
        self.experiment_runner = experiment_runner or ExperimentRunner(
            reconciliation_verifier=experiment_reconciliation_verifier,
            max_concurrent_runs=experiment_max_concurrent_runs,
            max_total_concurrent_runs=experiment_max_total_concurrent_runs,
        )
        if experiment_runner is not None and experiment_reconciliation_verifier is not None:
            self.experiment_runner.set_reconciliation_verifier(
                experiment_reconciliation_verifier
            )
        for adapter in experiment_adapters or ():
            if adapter.domain in self.experiment_runner.adapters:
                raise ValueError(f"Experiment adapter already registered for domain {adapter.domain}")
            self.experiment_runner.register_adapter(adapter)
        if ExperimentDomain.ML_SUPERVISED not in self.experiment_runner.adapters:
            self.experiment_runner.register_adapter(MLExperimentAdapter(self))
        if ExperimentDomain.ORACLE_STRATEGY not in self.experiment_runner.adapters:
            from Forge.core.adapters.oracle_adapter import build_default_oracle_experiment_adapter
            self.experiment_runner.register_adapter(build_default_oracle_experiment_adapter())
        self.champion_registry = champion_registry or ChampionRegistry(
            domain_owners=domain_owners,
            authorization_verifier=promotion_verifier,
        )
        if champion_registry is not None and domain_owners:
            self.champion_registry.configure_domain_owners(domain_owners)
        self.champion_registry.configure_authorization_verifier(promotion_verifier)
        self.evaluator = Evaluator()
        self.benchmark = Benchmark()
        from Forge.core.oracle_improvement import OracleImprovementService
        self.oracle_improvement = OracleImprovementService(self)
        self.challenger_engine = ChallengerEngine(
            registry=self.champion_registry,
            experiment_runner=self.experiment_runner,
            evaluator=self.evaluator,
            benchmark=self.benchmark,
            reconciliation_verifier=experiment_reconciliation_verifier,
        )
        self.discovery_translator = DiscoveryTranslator(
            method_discovery=self.discovery,
            champion_registry=self.champion_registry,
            forge_agent=self,
            experiment_runner=self.experiment_runner,
            authorization_verifier=promotion_verifier,
        )
        self.drift_policy_resolver = DriftPolicyResolver([
            DriftPolicy(policy_id="forge-default-drift-v1")
        ])
        self.drift_monitor = DriftMonitor(self.registry, self.drift_policy_resolver)
        self.drift_orchestrator = DriftOrchestrator(
            challenger_engine=self.challenger_engine,
            discovery_translator=self.discovery_translator,
            champion_registry=self.champion_registry,
            hypothesis_queue=self.hypothesis_queue,
            policy_resolver=self.drift_policy_resolver,
            authorization_verifier=promotion_verifier,
        )
        self.promotion_gate = PromotionGate(
            registry=self.champion_registry,
            policy=DefaultPromotionPolicy(),
            aegis_config=AegisRequirementConfig(default_requirement="REQUIRED"),
            authorization_verifier=promotion_verifier,
            evidence_provider=self.challenger_engine,
        )

        # ---- Universal Training Institution (§26) ----
        self.prompt_adapter = PromptOptimizationAdapter()
        self.workflow_adapter = WorkflowOptimizationAdapter()
        self.config_adapter = ConfigOptimizationAdapter()
        # Register new adapters on the experiment runner
        for adapter in (self.prompt_adapter, self.workflow_adapter, self.config_adapter):
            if adapter.domain not in self.experiment_runner.adapters:
                self.experiment_runner.register_adapter(adapter)
        self.request_handler = RequestHandler(
            authorization_verifier=training_request_verifier,
            reconciliation_verifier=experiment_reconciliation_verifier,
        )
        for target_type, domain_name in _TARGET_DOMAIN_MAP.items():
            if any(domain.value == domain_name for domain in self.experiment_runner.adapters):
                self.request_handler.register_dispatch_callback(
                    target_type,
                    self._execute_training_contract_request,
                )
        self.reuse_registry = ReuseRegistry(
            chronicle_client=chronicle_client,
            compatibility_policy=reuse_compatibility_policy,
            authorization_verifier=reuse_authorization_verifier,
        )

    def _execute_training_contract_request(self, request, lifecycle) -> TrainingReport:
        """Execute a request only through its requesting agent's declared contract."""
        raw_contract = request.context.get("experiment_contract")
        if not isinstance(raw_contract, dict):
            raise ValueError("Training request context must contain an experiment_contract object")
        contract = ExperimentContract.model_validate(raw_contract)

        expected_domain = _TARGET_DOMAIN_MAP.get(request.target.target_type)
        if expected_domain is None or contract.domain.value != expected_domain:
            raise ValueError(
                f"Contract domain '{contract.domain.value}' does not match target "
                f"'{request.target.target_type.value}'"
            )
        if contract.created_by != request.requestor.agent_name:
            raise ValueError("Experiment contract creator must match the training requestor")

        request_data = request.data
        for field in ("dataset_id", "dataset_path", "data_fingerprint"):
            expected = getattr(request_data, field, None)
            actual = getattr(contract.data, field, None)
            if expected is not None and expected != actual:
                raise ValueError(f"Experiment contract {field} does not match the training request")
        requested_metrics = set(request.evaluation.required_metrics)
        declared_metrics = set(contract.evaluation.metric_specs)
        if not requested_metrics.issubset(declared_metrics):
            raise ValueError(
                "Experiment contract must declare all requested evaluation metrics: "
                + ", ".join(sorted(requested_metrics - declared_metrics))
            )
        primary_metric = request.objective.primary_metric
        if primary_metric and primary_metric not in declared_metrics:
            raise ValueError("Experiment contract must declare the request's primary metric")

        experiment_id = self.experiment_runner.submit_experiment(contract)
        lifecycle.experiment_id = experiment_id
        result = self.experiment_runner.execute_experiment(experiment_id)
        lifecycle.run_id = result.run_id
        lifecycle.result_id = result.result_id
        persisted_contract = self.experiment_runner.get_experiment(experiment_id)
        if persisted_contract is None:
            raise RuntimeError("Persisted experiment contract was not available after submission")

        if result.status != ExperimentStatus.COMPLETED:
            return TrainingReport(
                request_id=request.request_id,
                experiment_id=experiment_id,
                run_id=result.run_id,
                result_id=result.result_id,
                data_lineage={
                    "dataset_id": persisted_contract.data.dataset_id,
                    "dataset_fingerprint": persisted_contract.data.data_fingerprint,
                },
                configuration_lineage={
                    "configuration_fingerprint": persisted_contract.environment.configuration_fingerprint,
                    "environment_fingerprint": result.evidence.get("environment_fingerprint"),
                },
                success=False,
                error_message=result.error_message or "Experiment adapter reported a failed run",
                promotion_eligible=False,
                outstanding_requirements=["Resolve the run failure before evaluation or promotion"],
            )

        evaluation = self.experiment_runner.get_evaluation_for_run(result.run_id)
        if evaluation is None:
            evaluation = self.evaluator.evaluate(persisted_contract, result)
            evaluation = self.experiment_runner.record_evaluation(evaluation)
        return TrainingReport(
            request_id=request.request_id,
            experiment_id=experiment_id,
            run_id=result.run_id,
            result_id=result.result_id,
            data_lineage={
                "dataset_id": persisted_contract.data.dataset_id,
                "dataset_fingerprint": persisted_contract.data.data_fingerprint,
            },
            configuration_lineage={
                "configuration_fingerprint": persisted_contract.environment.configuration_fingerprint,
                "environment_fingerprint": result.evidence.get("environment_fingerprint"),
                "random_seed": persisted_contract.environment.random_seed,
            },
            evaluation_results=evaluation.model_dump(mode="json"),
            benchmark_results={"metrics": result.metrics},
            statistical_summary={
                "statistical_evidence_complete": evaluation.statistical_evidence_complete,
                "statistically_improved_metrics": evaluation.statistically_improved_metrics,
                "statistically_degraded_metrics": evaluation.statistically_degraded_metrics,
            },
            reproducibility_info={
                "environment_fingerprint": result.evidence.get("environment_fingerprint"),
                "adapter_provenance": result.evidence.get("adapter_provenance"),
                "run_id": result.run_id,
            },
            success=True,
            promotion_eligible=False,
            outstanding_requirements=[
                "The owning agent must review the evidence and use its governed promotion process"
            ],
        )

    def on_start(self) -> None:
        log.info("Forge governed training platform online. Backends: %s | Atlas: %s",
                 [n for n, b in self._backends.items() if b.available], self.atlas is not None)
        if self.chronicle is not None:
            try:
                result = self.knowledge_outbox.drain(self.chronicle)
                if result["failed"]:
                    log.error("Chronicle outbox startup retry had delivery failures: %s", result)
                elif result["delivered"]:
                    log.info("Chronicle outbox startup retry delivered %d memories", result["delivered"])
            except Exception:
                log.exception("Chronicle outbox startup retry failed")

    # ---- cross-validated score for any backend (used by tuning + discovery) ----

    def _cv_score(
        self, ds: Dataset, backend_name: str, hp: Optional[Dict] = None, random_seed: int = 42
    ) -> Optional[float]:
        backend = self._backends.get(backend_name)
        if backend is None or not backend.available or backend.kind == "reinforcement":
            return None
        hp = hp or {}
        folds = kfold_indices(ds, k=min(5, max(2, ds.n_samples // 20)), seed=random_seed)
        scores = []
        for tr, va in folds:
            Xtr = [ds.X[i] for i in tr]; ytr = [ds.y[i] for i in tr]
            Xv = [ds.X[i] for i in va]; yv = [ds.y[i] for i in va]
            Xtr_s, Xv_s, _ = standardize(Xtr, Xv)
            try:
                raw, info = backend.train({"X_train": Xtr_s, "y_train": ytr,
                                         "task_type": ds.task_type,
                                         "random_seed": random_seed}, hp)
                preds = (predict_with(backend_name, raw, Xv_s)
                        if backend_name in ("xgboost", "lightgbm", "svm")
                        else UnifiedModel(backend_name, raw, ds.task_type,
                                        classes=info.get("classes")).predict(Xv_s))
                scores.append(self._score(preds, yv, ds.task_type))
            except Exception:
                return None
        return sum(scores) / len(scores) if scores else None

    def _score(self, preds, y, task_type) -> float:
        if task_type == "classification":
            return sum(1 for a, b in zip(preds, y) if a == b) / len(y) if y else 0.0
        n = len(y); mean_y = sum(y) / n
        ss_tot = sum((v - mean_y) ** 2 for v in y) or 1.0
        ss_res = sum((a - b) ** 2 for a, b in zip(preds, y))
        return 1 - ss_res / ss_tot

    def _governed_preferred_method(self, ds: Dataset) -> tuple[Optional[str], Optional[str]]:
        """Read an adopted method only from the authoritative V2 champion registry."""
        size = "small" if ds.n_samples < 200 else "medium" if ds.n_samples < 2000 else "large"
        dimension = "lowdim" if ds.n_features < 20 else "highdim"
        context_key = f"{ds.task_type}:{size}:{dimension}"
        incumbents = [
            champion for champion in self.champion_registry.list_champions("ML_SUPERVISED")
            if champion.context_key == context_key and champion.current_tenure is not None
        ]
        if len(incumbents) > 1:
            return None, f"multiple V2 incumbents exist for context '{context_key}'"
        if not incumbents:
            return None, None
        tenure = incumbents[0].current_tenure
        method = tenure.candidate_config.get("backend") or tenure.candidate_id
        if not isinstance(method, str) or not method.strip():
            return None, f"V2 incumbent for '{context_key}' has no valid backend identity"
        return method, None

    # ---- the self-improvement loop ----

    def evolve(self, ds: Dataset) -> Dict[str, Any]:
        """Discover method proposals without changing any incumbent preference.

        V1's ``MethodDiscovery.challenge`` persists a new preferred method
        directly from a single CV score. That bypasses Forge V2's experiment,
        evaluation, promotion, and authorization controls, so evolution stays
        proposal-only until the V2 challenge path is wired into this agent.
        """
        disc = self.discovery.discover(ds.task_type, ds.n_samples, ds.n_features)
        proposals = [m["method"] for m in disc["runnable_now"]]
        for base in ("sklearn", "xgboost", "lightgbm"):
            if base not in proposals and self._backends.get(base) and self._backends[base].available:
                proposals.append(base)
        return {
            "status": "complete",
            "mode": "proposal_only",
            "discovery": disc,
            "proposed_methods": proposals,
            "adoption_performed": False,
            "reason": "Candidate discovery does not authorize a champion change.",
            "recommended_but_missing": disc["recommended_but_missing"],
        }

    def train(self, ds: Dataset, user_backend=None, register_as=None, optimize=True,
              use_champion=True, hyperparameters: Optional[Dict[str, Any]] = None,
              random_seed: int = 42, bootstrap_replicates: int = 1000,
              include_evaluation_bootstrap: bool = False) -> Dict[str, Any]:
        if isinstance(random_seed, bool) or not isinstance(random_seed, int) or not 0 <= random_seed <= 2**32 - 1:
            return {"status": "error", "message": "random_seed must be an integer from 0 through 4294967295"}
        if include_evaluation_bootstrap and (
            isinstance(bootstrap_replicates, bool)
            or not isinstance(bootstrap_replicates, int)
            or not 200 <= bootstrap_replicates <= 20000
        ):
            return {
                "status": "error",
                "message": "bootstrap_replicates must be an integer from 200 through 20000",
            }
        validation = self.validator.validate(ds)
        if not validation["ok"]:
            return {"status": "error", "message": "data failed validation", "validation": validation}

        backend_name = user_backend
        if backend_name is None and use_champion:
            backend_name, champion_error = self._governed_preferred_method(ds)
            if champion_error:
                return {"status": "error", "message": champion_error}
        decision = None
        if backend_name is None:
            decision = self.selector.select(ds.task_type, ds.n_samples, ds.n_features,
                                           user_preference=user_backend)
            backend_name = decision["chosen_backend"]

        backend = self._backends.get(backend_name)
        if backend is None or not backend.available:
            return {"status": "error", "message": f"backend {backend_name} unavailable"}

        train_indices, validation_indices, split_method = train_test_indices(
            ds, 0.2, seed=random_seed
        )
        X_train = [ds.X[index] for index in train_indices]
        X_val = [ds.X[index] for index in validation_indices]
        y_train = [ds.y[index] for index in train_indices]
        y_val = [ds.y[index] for index in validation_indices]
        evaluation_holdout_fingerprint = self._fingerprint_evaluation_rows(
            ds, validation_indices
        )
        X_train_s, X_val_s, scaler = standardize(X_train, X_val)
        tuning_ds = Dataset(
            dataset_id=f"{ds.dataset_id}:train-only",
            X=X_train,
            y=y_train,
            feature_names=list(ds.feature_names),
            task_type=ds.task_type,
        )

        best_hp, opt_result = {}, None
        if hyperparameters is not None:
            # V2 Phase 2 Extension: Explicit hyperparameters override automatic optimization
            best_hp = hyperparameters
            opt_result = {"best_hyperparameters": best_hp, "source": "explicit_override"}
        elif optimize and backend.kind != "reinforcement":
            # Existing V1 optimizer logic (unchanged)
            opt_result = self.optimizer.optimize(
                backend_name,
                lambda hp: self._cv_score(tuning_ds, backend_name, hp, random_seed) or 0.0,
                n_trials=6,
                seed=random_seed,
            )
            best_hp = opt_result["best_hyperparameters"]

        raw, info = backend.train({"X_train": X_train_s, "y_train": y_train,
                                 "task_type": ds.task_type,
                                 "random_seed": random_seed}, best_hp)
        if backend_name in ("xgboost", "lightgbm", "svm"):
            preds = predict_with(backend_name, raw, X_val_s)
            metrics = self._metrics(preds, y_val, ds.task_type)
            model = _ExtraModelWrapper(backend_name, raw, ds.task_type, scaler)
        else:
            model = UnifiedModel(backend_name, raw, ds.task_type, classes=info.get("classes"), scaler=scaler)
            preds = list(model.predict(X_val_s))
            metrics = evaluate_supervised(model, X_val_s, y_val)
        bootstrap_metrics = None
        if include_evaluation_bootstrap:
            try:
                bootstrap_metrics = bootstrap_metric_samples(
                    y_val, preds, ds.task_type, seed=random_seed,
                    replicates=bootstrap_replicates,
                )
            except (TypeError, ValueError) as exc:
                return {"status": "error", "message": f"bootstrap evidence failed: {exc}"}

        lesson_name = register_as or ds.dataset_id
        if not isinstance(lesson_name, str) or not lesson_name.strip():
            lesson_name = str(lesson_name or "unnamed_dataset")
        lesson = f"Forge trained '{lesson_name}' via {backend_name}: {metrics}"
        eid = self.registry.record_experiment({"dataset": ds.profile(), "backend": backend_name,
            "hyperparameters": best_hp, "metrics": metrics, "decision": decision,
            "validation": validation, "cv_optimization": opt_result,
            "random_seed": random_seed}, knowledge_lesson={
                "content": lesson,
                "backend": backend_name,
                "model_name": lesson_name,
            })
        result = {"status": "complete", "experiment_id": eid, "chosen_backend": backend_name,
                 "metrics": metrics, "best_hyperparameters": best_hp,
                 "random_seed": random_seed,
                 "data_quality": validation["quality_score"], "used_champion": use_champion and decision is None}
        if bootstrap_metrics is not None:
            result["evaluation_bootstrap"] = {
                "method": "paired_percentile_bootstrap_v1",
                "replicates": len(next(iter(bootstrap_metrics.values()))),
                "sample_count": len(y_val),
                "metrics": bootstrap_metrics,
            }
            result["evaluation_partition"] = {
                "method": split_method,
                "random_seed": random_seed,
                "sample_count": ds.n_samples,
                "validation_sample_count": len(validation_indices),
                "holdout_fingerprint": evaluation_holdout_fingerprint,
                "train_indices_fingerprint": hashlib.sha256(json.dumps(
                    train_indices, separators=(",", ":"), allow_nan=False
                ).encode("utf-8")).hexdigest(),
                "validation_indices_fingerprint": hashlib.sha256(json.dumps(
                    validation_indices, separators=(",", ":"), allow_nan=False
                ).encode("utf-8")).hexdigest(),
            }
        if register_as:
            rec = self.registry.register_model(register_as, "1.0.0", ds.dataset_id, ds.task_type,
                                              metrics, eid, model_obj=model, hyperparams=best_hp,
                                              feature_baseline=scaler)
            result["model_id"] = rec["model_id"]
        self._drain_knowledge_outbox()
        return result

    @staticmethod
    def _fingerprint_evaluation_rows(ds: Dataset, indices: List[int]) -> str:
        """Hash the exact raw observations used for validation, independent of row offsets."""
        observation_digests = []
        for index in indices:
            value = ds.y[index]
            if hasattr(value, "item") and callable(value.item):
                value = value.item()
            if value is None:
                label = {"type": "null", "value": None}
            elif isinstance(value, bool):
                label = {"type": "bool", "value": value}
            elif isinstance(value, int):
                label = {"type": "int", "value": value}
            elif isinstance(value, float) and math.isfinite(value):
                label = {"type": "float", "value": value}
            elif isinstance(value, str):
                label = {"type": "str", "value": value}
            else:
                raise ValueError("Evaluation target contains a non-canonical label value")
            observation = {
                "features": [float(value) for value in ds.X[index]],
                "target": label,
            }
            canonical_observation = json.dumps(
                observation, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")
            observation_digests.append(hashlib.sha256(canonical_observation).hexdigest())
        observation_digests.sort()
        payload = {
            "schema": "forge_ml_evaluation_holdout_v1",
            "task_type": ds.task_type,
            "feature_names": list(ds.feature_names),
            "observation_digests": observation_digests,
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _metrics(self, preds, y, task_type):
        if task_type == "classification":
            try:
                from sklearn.metrics import accuracy_score, precision_recall_fscore_support
                acc = accuracy_score(y, preds)
                p, r, f1, _ = precision_recall_fscore_support(y, preds, average="weighted", zero_division=0)
                return {"accuracy": round(float(acc), 4), "precision": round(float(p), 4),
                       "recall": round(float(r), 4), "f1": round(float(f1), 4)}
            except Exception:
                return {"accuracy": round(sum(1 for a, b in zip(preds, y) if a == b) / len(y), 4)}
        n = len(y); mean_y = sum(y) / n
        ss_tot = sum((v - mean_y) ** 2 for v in y) or 1.0
        ss_res = sum((a - b) ** 2 for a, b in zip(preds, y))
        return {"r2": round(1 - ss_res / ss_tot, 4),
               "mae": round(sum(abs(a - b) for a, b in zip(preds, y)) / n, 4),
               "rmse": round((ss_res / n) ** 0.5, 4)}

    # ---- BaseAgent contract ----

    def execute(self, task: str, context: Dict[str, Any]) -> Dict[str, Any]:
        ctx = context
        if task == "oracle.improvement.request":
            return self.oracle_improvement.request(ctx.get("request", {}))
        if task == "knowledge.outbox.drain":
            try:
                result = self.knowledge_outbox.drain(
                    self.chronicle, limit=ctx.get("limit", 25)
                )
                return {"status": "complete", **result}
            except Exception as exc:
                log.exception("manual Chronicle outbox drain failed")
                return {"status": "error", "message": type(exc).__name__}
        if task == "hypothesis.add":
            statement = ctx.get("statement")
            proposed_by = ctx.get("proposed_by", "unknown")
            if not statement:
                return {"status": "error", "message": "statement is required"}
            h = self.hypothesis_queue.add(statement, proposed_by, ctx.get("hypothesis_id"))
            return {"status": "complete", "hypothesis": h.to_dict()}

        if task == "hypothesis.list":
            status_filter = ctx.get("status")
            hyps = self.hypothesis_queue.all()
            if status_filter:
                hyps = [h for h in hyps if h.status == status_filter]
            return {"status": "complete", "hypotheses": [h.to_dict() for h in hyps]}

        if task == "experiment.contract.submit":
            try:
                contract = ExperimentContract.model_validate(ctx.get("contract", {}))
                experiment_id = self.experiment_runner.submit_experiment(contract)
                return {"status": "complete", "experiment_id": experiment_id}
            except Exception as exc:
                return {"status": "error", "message": f"experiment contract submission failed: {type(exc).__name__}: {exc}"}

        if task in ("experiment.contract.execute", "experiment.contract.evaluate"):
            try:
                if task.endswith(".execute"):
                    experiment_id = ctx.get("experiment_id")
                    if not experiment_id:
                        return {"status": "error", "message": "experiment_id is required"}
                    result = self.experiment_runner.execute_experiment(experiment_id)
                    if result.status != ExperimentStatus.COMPLETED:
                        return {"status": "complete", "result": result.model_dump(mode="json"),
                                "evaluation_report": None}
                    contract = self.experiment_runner.get_experiment(experiment_id)
                else:
                    run_id = ctx.get("run_id")
                    if not run_id:
                        return {"status": "error", "message": "run_id is required"}
                    run = self.experiment_runner.get_run(run_id)
                    if run is None:
                        return {"status": "error", "message": f"run {run_id} not found"}
                    result = self.experiment_runner.get_result_for_run(run_id)
                    contract = self.experiment_runner.get_experiment(run.experiment_id)
                    if result is None or result.status != ExperimentStatus.COMPLETED:
                        return {"status": "error", "message": "completed persisted result is required for evaluation"}
                if contract is None:
                    return {"status": "error", "message": "experiment contract was not found"}
                report = self.experiment_runner.get_evaluation_for_run(result.run_id)
                if report is None:
                    report = self.evaluator.evaluate(contract, result)
                    report = self.experiment_runner.record_evaluation(report)
                return {
                    "status": "complete",
                    "result": result.model_dump(mode="json"),
                    "evaluation_report": report.model_dump(mode="json"),
                    "promotion_performed": False,
                }
            except Exception as exc:
                return {"status": "error", "message": f"experiment execution/evaluation failed: {type(exc).__name__}: {exc}"}

        if task == "experiment.contract.report":
            experiment_id = ctx.get("experiment_id")
            if not experiment_id:
                return {"status": "error", "message": "experiment_id is required"}
            contract = self.experiment_runner.get_experiment(experiment_id)
            if contract is None:
                return {"status": "error", "message": f"experiment {experiment_id} not found"}
            run_reports = []
            for run in self.experiment_runner.list_runs(experiment_id):
                result = self.experiment_runner.get_result_for_run(run.run_id)
                evaluation = self.experiment_runner.get_evaluation_for_run(run.run_id)
                run_reports.append({
                    "run": run.model_dump(mode="json"),
                    "result": result.model_dump(mode="json") if result else None,
                    "evaluation_report": evaluation.model_dump(mode="json") if evaluation else None,
                })
            return {"status": "complete", "contract": contract.model_dump(mode="json"),
                    "runs": run_reports, "promotion_performed": False}

        if task == "experiment.run":
            # Runs one Experiment Template (currently: sensitivity_analysis)
            # against caller-supplied data -- Forge doesn't know or care
            # which agent's data this is or what it means; that's the whole
            # point of the generalized (value, outcome) interface.
            template_name = ctx.get("template")
            hypothesis_id = ctx.get("hypothesis_id")
            template_kwargs = ctx.get("template_kwargs", {})
            if not template_name:
                return {"status": "error", "message": "template is required"}
            result = run_template(template_name, **template_kwargs)
            response: Dict[str, Any] = {"status": "complete", "result": result}
            if hypothesis_id:
                try:
                    self.hypothesis_queue.mark_testing(hypothesis_id)
                    updated = self.hypothesis_queue.record_evidence(
                        hypothesis_id, template_name, result,
                        sample_size=result.get("sample_size", 0))
                    response["hypothesis"] = updated.to_dict()
                except KeyError as exc:
                    response["hypothesis_error"] = str(exc)
            return response

        if task == "backends.catalog":
            return {"status": "complete",
                   "catalog": {n: b.describe() for n, b in self._backends.items()}}
        if task == "method.discover":
            return {"status": "complete", "discovery": self.discovery.discover(
                ctx.get("task_type", "classification"), ctx.get("n_samples", 100),
                ctx.get("n_features", 10))}
        if task == "evolution.discover":
            try:
                profile = DiscoveryProfile.model_validate(ctx.get("profile", ctx))
                discovery_result = self.discovery_translator.generate_challenges(profile)
                return {
                    "status": "complete",
                    "initialization_required": discovery_result.initialization_required,
                    "initialization_reason": discovery_result.initialization_reason,
                    "candidates_discovered": discovery_result.candidates_discovered,
                    "candidates_skipped": discovery_result.candidates_skipped,
                    "skipped_reasons": discovery_result.skipped_reasons,
                    "challenges": [
                        {
                            "request": request.model_dump(mode="json"),
                            "benchmark_context": benchmark_context.model_dump(mode="json"),
                        }
                        for request, benchmark_context in discovery_result.challenges
                    ],
                }
            except Exception as exc:
                return {"status": "error", "message": f"challenge discovery failed: {type(exc).__name__}: {exc}"}
        if task == "evolution.bootstrap.prepare":
            try:
                profile = DiscoveryProfile.model_validate(ctx.get("profile", ctx))
                request = self.discovery_translator.prepare_v2_champion(
                    profile, backend=ctx.get("backend", "sklearn")
                )
                return {
                    "status": "prepared",
                    "bootstrap_request": request.model_dump(mode="json"),
                    "next_task": "evolution.bootstrap.authorization_request",
                }
            except Exception as exc:
                return {"status": "error", "message": f"champion bootstrap preparation failed: {type(exc).__name__}: {exc}"}
        if task == "evolution.bootstrap.authorization_request":
            try:
                record = self.champion_registry.get_champion_bootstrap(ctx.get("bootstrap_id", ""))
                if record is None:
                    return {"status": "error", "message": "persisted champion bootstrap was not found"}
                if record["status"] != "PREPARED":
                    return {"status": "error", "message": f"champion bootstrap is {record['status'].lower()}"}
                authorized_by = ctx.get("authorized_by")
                if not isinstance(authorized_by, str) or not authorized_by.strip():
                    return {"status": "error", "message": "authorized_by is required for the Aegis claim request"}
                return {
                    "status": "complete",
                    "bootstrap_id": record["request"].bootstrap_id,
                    "audience": "forge.promotion",
                    "required_claims": self.discovery_translator.bootstrap_authorization_claims(
                        record["request"], authorized_by
                    ),
                }
            except Exception as exc:
                return {"status": "error", "message": f"bootstrap authorization request failed: {type(exc).__name__}: {exc}"}
        if task == "evolution.bootstrap.commit":
            try:
                authorization = AuthorizationToken.model_validate(ctx["authorization"])
                champion = self.discovery_translator.commit_v2_champion_bootstrap(
                    ctx["bootstrap_id"], authorization
                )
                return {"status": "complete", "champion": champion.model_dump(mode="json")}
            except Exception as exc:
                return {"status": "error", "message": f"champion bootstrap commit failed: {type(exc).__name__}: {exc}"}
        if task == "evolution.challenge.run":
            try:
                request = ChallengeRequest.model_validate(ctx["request"])
                benchmark_context = BenchmarkContext.model_validate(ctx["benchmark_context"])
                challenge_result = self.challenger_engine.run(request, benchmark_context)
                evaluation_report = (
                    self.challenger_engine.get_evaluation_report(challenge_result.evaluation_report_id)
                    if challenge_result.evaluation_report_id else None
                )
                benchmark_report = (
                    self.challenger_engine.get_benchmark_report(challenge_result.benchmark_report_id)
                    if challenge_result.benchmark_report_id else None
                )
                return {
                    "status": "complete" if challenge_result.status.value == "COMPLETED" else "error",
                    "challenge_result": challenge_result.model_dump(mode="json"),
                    "evaluation_report": evaluation_report.model_dump(mode="json") if evaluation_report else None,
                    "benchmark_report": benchmark_report.model_dump(mode="json") if benchmark_report else None,
                }
            except Exception as exc:
                return {"status": "error", "message": f"challenge execution failed: {type(exc).__name__}: {exc}"}
        if task == "evolution.promotion.evaluate":
            try:
                challenge_result = self.challenger_engine.get_result(ctx["challenge_id"])
                if challenge_result is None:
                    return {"status": "error", "message": "challenge was not found"}
                evaluation_report = (
                    self.challenger_engine.get_evaluation_report(challenge_result.evaluation_report_id)
                    if challenge_result.evaluation_report_id else None
                )
                benchmark_report = (
                    self.challenger_engine.get_benchmark_report(challenge_result.benchmark_report_id)
                    if challenge_result.benchmark_report_id else None
                )
                if evaluation_report is None or benchmark_report is None:
                    return {"status": "error", "message": "challenge lacks complete persisted evaluation evidence"}
                decision = self.promotion_gate.evaluate(
                    challenge_result,
                    evaluation_report,
                    benchmark_report,
                    risk_category=ctx.get("risk_category", "general"),
                )
                return {"status": "complete", "promotion_decision": decision.model_dump(mode="json")}
            except Exception as exc:
                return {"status": "error", "message": f"promotion evaluation failed: {type(exc).__name__}: {exc}"}
        if task == "evolution.promotion.authorization_request":
            decision_record = self.champion_registry.get_promotion_decision(ctx.get("decision_id", ""))
            if decision_record is None:
                return {"status": "error", "message": "persisted promotion decision was not found"}
            if decision_record["status"] != "EVALUATED":
                return {"status": "error", "message": f"promotion decision is {decision_record['status'].lower()}"}
            decision = PromotionDecision.model_validate(decision_record["decision"])
            authorized_by = ctx.get("authorized_by")
            if not isinstance(authorized_by, str) or not authorized_by.strip():
                return {"status": "error", "message": "authorized_by is required for the Aegis claim request"}
            return {
                "status": "complete",
                "decision_id": decision.decision_id,
                "audience": "forge.promotion",
                "required_claims": self.promotion_gate.authorization_claims(decision, authorized_by),
            }
        if task == "evolution.promotion.commit":
            try:
                decision_record = self.champion_registry.get_promotion_decision(ctx["decision_id"])
                if decision_record is None:
                    return {"status": "error", "message": "persisted promotion decision was not found"}
                decision = PromotionDecision.model_validate(decision_record["decision"])
                authorization = AuthorizationToken.model_validate(ctx["authorization"])
                commit_result = self.promotion_gate.commit(decision, authorization)
                return {
                    "status": "complete" if commit_result.success else "error",
                    "commit_result": commit_result.model_dump(mode="json"),
                }
            except Exception as exc:
                return {"status": "error", "message": f"promotion commit failed: {type(exc).__name__}: {exc}"}
        if task == "evolution.status":
            return {"status": "complete", **self._operational_snapshot()}
        if task == "experiment.reconciliation.authorization_request":
            try:
                claims = self.experiment_runner.run_reconciliation_claims(
                    run_id=ctx["run_id"],
                    authorized_by=ctx["authorized_by"],
                    reason=ctx["reason"],
                    worker_terminated=ctx.get("worker_terminated"),
                )
                return {
                    "status": "complete",
                    "authorization_request": {
                        "audience": "forge.operations",
                        "required_claims": claims,
                        "required_authorization_fields": [
                            "authorization_id", "run_id", "experiment_id", "authorized_by",
                            "decision", "audience", "issuer", "issued_at", "expires_at",
                            "nonce", "signature", "claims",
                        ],
                        "signature_payload": (
                            "Canonical UTF-8 JSON of the authorization model with signature omitted"
                        ),
                    },
                }
            except Exception as exc:
                return {
                    "status": "error",
                    "message": f"run reconciliation authorization request failed: {type(exc).__name__}: {exc}",
                }
        if task == "experiment.reconciliation.reconcile":
            try:
                authorization = RunReconciliationAuthorization.model_validate(
                    ctx["authorization"]
                )
                result = self.experiment_runner.reconcile_abandoned_run(
                    run_id=ctx["run_id"], authorization=authorization
                )
                return {
                    "status": "complete",
                    "result": result.model_dump(mode="json"),
                }
            except Exception as exc:
                return {
                    "status": "error",
                    "message": f"run reconciliation failed: {type(exc).__name__}: {exc}",
                }
        if task == "evolution.challenge.reconciliation.authorization_request":
            try:
                claims = self.challenger_engine.challenge_reconciliation_claims(
                    challenge_id=ctx["challenge_id"],
                    authorized_by=ctx["authorized_by"],
                    reason=ctx["reason"],
                    worker_terminated=ctx.get("worker_terminated"),
                )
                return {
                    "status": "complete",
                    "authorization_request": {
                        "audience": "forge.operations",
                        "required_claims": claims,
                        "required_authorization_fields": [
                            "authorization_id", "challenge_id", "authorized_by",
                            "decision", "audience", "issuer", "issued_at", "expires_at",
                            "nonce", "signature", "claims",
                        ],
                        "signature_payload": (
                            "Canonical UTF-8 JSON of the authorization model with signature omitted"
                        ),
                    },
                }
            except Exception as exc:
                return {
                    "status": "error",
                    "message": f"challenge reconciliation authorization request failed: {type(exc).__name__}: {exc}",
                }
        if task == "evolution.challenge.reconciliation.reconcile":
            try:
                authorization = ChallengeReconciliationAuthorization.model_validate(
                    ctx["authorization"]
                )
                result = self.challenger_engine.reconcile_abandoned_challenge(
                    challenge_id=ctx["challenge_id"], authorization=authorization
                )
                return {
                    "status": "complete",
                    "result": result.model_dump(mode="json"),
                }
            except Exception as exc:
                return {
                    "status": "error",
                    "message": f"challenge reconciliation failed: {type(exc).__name__}: {exc}",
                }
        if task == "evolution.challenge.resume.authorization_request":
            try:
                claims = self.challenger_engine.challenge_resume_claims(
                    challenge_id=ctx["challenge_id"],
                    run_id=ctx["run_id"],
                    authorized_by=ctx["authorized_by"],
                    reason=ctx["reason"],
                    worker_terminated=ctx.get("worker_terminated"),
                )
                return {
                    "status": "complete",
                    "authorization_request": {
                        "audience": "forge.operations",
                        "required_claims": claims,
                        "required_authorization_fields": [
                            "authorization_id", "challenge_id", "authorized_by",
                            "decision", "audience", "issuer", "issued_at", "expires_at",
                            "nonce", "signature", "claims",
                        ],
                        "signature_payload": (
                            "Canonical UTF-8 JSON of the authorization model with signature omitted"
                        ),
                    },
                }
            except Exception as exc:
                return {
                    "status": "error",
                    "message": f"challenge resume authorization request failed: {type(exc).__name__}: {exc}",
                }
        if task == "evolution.challenge.resume":
            try:
                authorization = ChallengeReconciliationAuthorization.model_validate(
                    ctx["authorization"]
                )
                result = self.challenger_engine.resume_abandoned_challenge(
                    challenge_id=ctx["challenge_id"],
                    run_id=ctx["run_id"],
                    authorization=authorization,
                )
                return {"status": "complete", "result": result.model_dump(mode="json")}
            except Exception as exc:
                return {
                    "status": "error",
                    "message": f"challenge resume failed: {type(exc).__name__}: {exc}",
                }
        if task == "drift.assess":
            try:
                report = self.drift_monitor.detect(
                    model_id=ctx["model_id"],
                    live_features=ctx.get("live_features", []),
                    observation_window_start=_parse_datetime(ctx.get("observation_window_start")),
                    observation_window_end=_parse_datetime(ctx.get("observation_window_end")),
                    champion_id=ctx.get("champion_id"),
                    feature_names=ctx.get("feature_names"),
                )
                suggestion = self.drift_orchestrator.process_report(report)
                return {
                    "status": "complete",
                    "drift_report": report.model_dump(mode="json"),
                    "investigation_suggestion": suggestion.model_dump(mode="json"),
                }
            except Exception as exc:
                return {"status": "error", "message": f"drift assessment failed: {type(exc).__name__}: {exc}"}
        if task == "drift.investigation.authorization_request":
            suggestion = self.drift_orchestrator.get_persisted_suggestion(ctx.get("report_id", ""))
            if suggestion is None:
                return {"status": "error", "message": "persisted drift assessment was not found"}
            authorized_by = ctx.get("authorized_by")
            if not isinstance(authorized_by, str) or not authorized_by.strip():
                return {"status": "error", "message": "authorized_by is required for the Aegis claim request"}
            if suggestion.suggested_investigation.value == "monitor_only":
                return {"status": "error", "message": "this drift assessment does not recommend an investigation"}
            return {
                "status": "complete",
                "report_id": suggestion.report_id,
                "audience": "forge.promotion",
                "required_claims": self.drift_orchestrator.authorization_claims(suggestion, authorized_by),
            }
        if task == "drift.investigation.execute":
            try:
                suggestion = self.drift_orchestrator.get_persisted_suggestion(ctx["report_id"])
                if suggestion is None:
                    return {"status": "error", "message": "persisted drift assessment was not found"}
                authorization = AuthorizationToken.model_validate(ctx["authorization"])
                investigation_id = self.drift_orchestrator.execute_investigation(
                    suggestion, authorization
                )
                return {
                    "status": "complete",
                    "investigation_id": investigation_id,
                    "model_key": suggestion.champion_id or suggestion.model_id,
                }
            except Exception as exc:
                return {"status": "error", "message": f"drift investigation authorization failed: {type(exc).__name__}: {exc}"}
        if task == "drift.investigation.complete":
            try:
                self.drift_orchestrator.complete_investigation(
                    ctx["model_key"], ctx["investigation_id"]
                )
                return {"status": "complete"}
            except Exception as exc:
                return {"status": "error", "message": f"investigation completion failed: {type(exc).__name__}: {exc}"}
        if task == "training.evolve":
            ds = self._ds(ctx)
            if ds is None:
                return {"status": "error", "message": "provide data"}
            return self.evolve(ds)
        if task in ("training.run", "training.from_csv"):
            ds = self._ds(ctx)
            if ds is None:
                return {"status": "error", "message": "provide X+y or path+target_column"}
            return self.train(ds, user_backend=ctx.get("backend"),
                            register_as=ctx.get("register_as"), optimize=ctx.get("optimize", True),
                            hyperparameters=ctx.get("hyperparameters"),
                            random_seed=ctx.get("random_seed", 42),
                            bootstrap_replicates=ctx.get("bootstrap_replicates", 1000),
                            include_evaluation_bootstrap=ctx.get(
                                "include_evaluation_bootstrap", False
                            ))
        if task == "data.validate":
            ds = self._ds(ctx)
            return ({"status": "complete", "validation": self.validator.validate(ds)}
                   if ds else {"status": "error", "message": "provide data"})
        if task == "model.leaderboard":
            return {"status": "complete", "leaderboard": self.registry.leaderboard(
                ctx.get("domain", ""), ctx.get("metric", "accuracy"))}
        if task == "model.drift":
            return {"status": "complete", **self.registry.detect_drift(
                ctx.get("model_id", ""), ctx.get("live_features", []))}
        if task == "model.predict":
            model_id = ctx.get("model_id", "")
            if "X" not in ctx:
                return {"status": "error", "message": "X is required for model prediction"}
            record = self.registry.get_model(model_id)
            if record is None:
                return {"status": "error", "message": "model not found"}
            active_model_id = self.deployment_controller.active_model(record["domain"])
            if active_model_id != model_id:
                return {
                    "status": "error",
                    "message": (
                        "model is not active for serving; use model.predict_active "
                        "to follow governed shadow and canary routing"
                    ),
                }
            approved_digest = self.deployment_controller.approved_artifact_digest(
                record["domain"], model_id
            )
            model = self.registry.load_model_for_deployment(model_id, approved_digest)
            return ({"status": "complete", "model_id": model_id,
                     "predictions": list(model.predict(ctx["X"]))}
                   if model else {"status": "error", "message": "active model artifact does not match its approved deployment digest"})
        if task == "model.predict_active":
            try:
                if "X" not in ctx:
                    return {"status": "error", "message": "X is required for active-model prediction"}
                route = self.deployment_controller.route(
                    domain=ctx.get("domain", "ML_SUPERVISED"),
                    request_key=ctx.get("request_key", ""),
                )
                if route.primary_model_id is None:
                    return {"status": "error", "message": "no active model for this domain"}
                approved_digest = self.deployment_controller.approved_artifact_digest(
                    route.domain, route.primary_model_id
                )
                model = self.registry.load_model_for_deployment(
                    route.primary_model_id, approved_digest
                )
                if model is None:
                    return {"status": "error", "message": "routed model artifact does not match its approved deployment digest"}
                predictions = list(model.predict(ctx.get("X", [])))
                result = {
                    "status": "complete",
                    "model_id": route.primary_model_id,
                    "predictions": predictions,
                    "routing": route.model_dump(mode="json"),
                }
                if route.shadow_model_id:
                    shadow_digest = self.deployment_controller.approved_artifact_digest(
                        route.domain, route.shadow_model_id
                    )
                    if shadow_digest is None:
                        result["shadow_observation_error"] = "approved_deployment_digest_unavailable"
                    else:
                        shadow = self.registry.load_model_for_deployment(
                            route.shadow_model_id, shadow_digest
                        )
                        if shadow is None:
                            result["shadow_observation_error"] = "artifact_failed_approved_digest_check"
                        else:
                            try:
                                result["shadow_observation"] = {
                                    "model_id": route.shadow_model_id,
                                    "predictions": list(shadow.predict(ctx.get("X", []))),
                                }
                            except Exception as exc:
                                result["shadow_observation_error"] = type(exc).__name__
                                log.exception(
                                    "Shadow prediction failed for deployment %s",
                                    route.deployment_id,
                                )
                return result
            except Exception as exc:
                return {"status": "error", "message": f"active-model routing failed: {exc}"}
        if task == "deployment.prepare_rollout":
            try:
                report = BenchmarkReport.model_validate(ctx["benchmark_report"])
                request = self.deployment_controller.prepare_rollout_authorization(
                    model_id=ctx["model_id"],
                    benchmark_report=report,
                    deployment_id=ctx.get("deployment_id"),
                )
                return {"status": "complete", "authorization_request": request.model_dump(mode="json")}
            except Exception as exc:
                return {"status": "error", "message": str(exc)}
        if task == "deployment.start_rollout":
            try:
                report = BenchmarkReport.model_validate(ctx["benchmark_report"])
                authorization = DeploymentAuthorization.model_validate(ctx["authorization"])
                record = self.deployment_controller.start_rollout(
                    model_id=ctx["model_id"],
                    benchmark_report=report,
                    authorization=authorization,
                    deployment_id=ctx.get("deployment_id"),
                )
                return {"status": "complete", "deployment": record.model_dump(mode="json")}
            except Exception as exc:
                return {"status": "error", "message": str(exc)}
        if task == "deployment.prepare_advance":
            try:
                request = self.deployment_controller.prepare_advance_authorization(
                    ctx["deployment_id"]
                )
                return {"status": "complete", "authorization_request": request.model_dump(mode="json")}
            except Exception as exc:
                return {"status": "error", "message": str(exc)}
        if task == "deployment.advance":
            try:
                authorization = DeploymentAuthorization.model_validate(ctx["authorization"])
                record = self.deployment_controller.advance(
                    deployment_id=ctx["deployment_id"],
                    request_id=ctx["request_id"],
                    authorization=authorization,
                )
                return {"status": "complete", "deployment": record.model_dump(mode="json")}
            except Exception as exc:
                return {"status": "error", "message": str(exc)}
        if task == "deployment.prepare_rollback":
            try:
                request = self.deployment_controller.prepare_rollback_authorization(
                    deployment_id=ctx["deployment_id"], reason=ctx["reason"]
                )
                return {"status": "complete", "authorization_request": request.model_dump(mode="json")}
            except Exception as exc:
                return {"status": "error", "message": str(exc)}
        if task == "deployment.rollback":
            try:
                authorization = DeploymentAuthorization.model_validate(ctx["authorization"])
                record = self.deployment_controller.rollback(
                    deployment_id=ctx["deployment_id"],
                    reason=ctx["reason"],
                    authorization=authorization,
                )
                return {"status": "complete", "deployment": record.model_dump(mode="json")}
            except Exception as exc:
                return {"status": "error", "message": str(exc)}
        if task == "deployment.status":
            deployment_id = ctx.get("deployment_id")
            record = (
                self.deployment_controller.get_deployment(deployment_id)
                if deployment_id else None
            )
            domain = ctx.get("domain") or (record.domain if record else None)
            return {
                "status": "complete",
                "deployment": record.model_dump(mode="json") if record else None,
                "active_model_id": self.deployment_controller.active_model(domain) if domain else None,
                "audit_integrity": self.deployment_controller.verify_audit_chain(),
                "rollout_stats": self.deployment_controller.stats(),
                "operational_status": self.deployment_controller.operational_status(),
            }
        if task == "model.promote":
            return {"status": "complete", **self.registry.promote(ctx.get("model_id", ""),
                    ctx.get("to_status", "candidate"), ctx.get("benchmark_passed", False))}

        # ==================================================================
        # Universal Training Institution (§26) — cross-agent training tasks
        # ==================================================================
        if task == "training.request.submit":
            try:
                raw = ctx.get("request", ctx)
                request = TrainingRequest.model_validate(raw)
                return self.request_handler.submit_request(request)
            except Exception as exc:
                return {"status": "error", "message": f"training request submission failed: {type(exc).__name__}: {exc}"}

        if task == "training.request.authorization_request":
            try:
                request_id = ctx.get("request_id")
                authorized_by = ctx.get("authorized_by")
                if not request_id or not authorized_by:
                    return {"status": "error", "message": "request_id and authorized_by are required"}
                request = self.request_handler.authorization_request(request_id, authorized_by)
                return {"status": "complete", "authorization_request": request}
            except Exception as exc:
                return {"status": "error", "message": f"request authorization preparation failed: {type(exc).__name__}: {exc}"}

        if task == "training.request.authorize":
            try:
                request_id = ctx.get("request_id")
                if not request_id or not ctx.get("authorization"):
                    return {"status": "error", "message": "request_id and authorization are required"}
                authorization = AuthorizationToken.model_validate(ctx["authorization"])
                return self.request_handler.authorize_request(request_id, authorization)
            except Exception as exc:
                return {"status": "error", "message": f"training request authorization failed: {type(exc).__name__}: {exc}"}

        if task == "training.request.reconciliation.authorization_request":
            try:
                claims = self.request_handler.request_reconciliation_claims(
                    request_id=ctx.get("request_id", ""),
                    authorized_by=ctx.get("authorized_by", ""),
                    reason=ctx.get("reason", ""),
                    worker_terminated=ctx.get("worker_terminated"),
                )
                return {"status": "complete", "authorization_request": {
                    "request_id": ctx["request_id"],
                    "audience": "forge.operations",
                    "required_claims": claims,
                    "required_fields": [
                        "authorization_id", "request_id", "authorized_by", "decision",
                        "audience", "issuer", "issued_at", "expires_at", "nonce",
                        "signature", "claims",
                    ],
                }}
            except Exception as exc:
                return {"status": "error", "message": f"request reconciliation preparation failed: {type(exc).__name__}: {exc}"}

        if task == "training.request.reconciliation.reconcile":
            try:
                request_id = ctx.get("request_id")
                if not request_id or not ctx.get("authorization"):
                    return {"status": "error", "message": "request_id and authorization are required"}
                authorization = RequestReconciliationAuthorization.model_validate(
                    ctx["authorization"]
                )
                return self.request_handler.reconcile_abandoned_request(
                    request_id, authorization
                )
            except Exception as exc:
                return {"status": "error", "message": f"request reconciliation failed: {type(exc).__name__}: {exc}"}

        if task == "training.request.status":
            request_id = ctx.get("request_id")
            if not request_id:
                return {"status": "error", "message": "request_id is required"}
            result = self.request_handler.get_request_status(request_id)
            if result is None:
                return {"status": "error", "message": f"request {request_id} not found"}
            return {"status": "complete", **result}

        if task == "training.request.report":
            request_id = ctx.get("request_id")
            if not request_id:
                return {"status": "error", "message": "request_id is required"}
            result = self.request_handler.get_report(request_id)
            if result is None:
                return {"status": "error", "message": f"no report available for request {request_id}"}
            return {"status": "complete", "report": result}

        if task == "training.request.cancel":
            request_id = ctx.get("request_id")
            if not request_id:
                return {"status": "error", "message": "request_id is required"}
            return self.request_handler.cancel_request(request_id)

        if task == "training.request.list":
            requests = self.request_handler.list_requests(
                agent_name=ctx.get("agent_name"),
                status=ctx.get("status"),
            )
            return {"status": "complete", "requests": requests}

        if task == "training.request.scheduler_status":
            return {
                "status": "complete",
                "scheduler": self.request_handler.scheduler_snapshot(),
                "durable_ledger": self.request_handler.operational_status(),
            }

        if task == "training.request.execute_next":
            result = self.request_handler.execute_next()
            if result is None:
                return {"status": "complete", "message": "no eligible request to execute"}
            return {"status": "complete", "result": result}

        if task == "training.reuse.search":
            try:
                from Forge.core.reuse_registry import ArtifactKind
                kind = None
                if ctx.get("kind"):
                    kind = ArtifactKind(ctx["kind"])
                artifacts = self.reuse_registry.search_artifacts(
                    kind=kind,
                    domain=ctx.get("domain"),
                    source_agent=ctx.get("source_agent"),
                )
                return {
                    "status": "complete",
                    "artifacts": [a.model_dump(mode="json") for a in artifacts],
                }
            except Exception as exc:
                return {"status": "error", "message": f"reuse search failed: {type(exc).__name__}: {exc}"}

        if task == "training.reuse.check":
            try:
                check = self.reuse_registry.check_compatibility(
                    artifact_id=ctx["artifact_id"],
                    requesting_agent=ctx["requesting_agent"],
                    target_domain=ctx["target_domain"],
                    target_types=ctx.get("target_types"),
                    required_permissions=ctx.get("required_permissions"),
                )
                return {"status": "complete", "check": check.model_dump(mode="json")}
            except Exception as exc:
                return {"status": "error", "message": f"compatibility check failed: {type(exc).__name__}: {exc}"}

        if task == "training.reuse.authorization_request":
            try:
                request = self.reuse_registry.reuse_authorization_request(
                    check_id=ctx["check_id"],
                    authorized_by=ctx["authorized_by"],
                    conditions=ctx.get("conditions"),
                )
                return {"status": "complete", "authorization_request": request}
            except Exception as exc:
                return {"status": "error", "message": f"reuse authorization request failed: {type(exc).__name__}: {exc}"}

        if task == "training.reuse.approve":
            try:
                authorization = ReuseAuthorization.model_validate(ctx["authorization"])
                record = self.reuse_registry.approve_reuse(
                    check_id=ctx["check_id"],
                    authorization=authorization,
                    conditions=ctx.get("conditions"),
                )
                return {"status": "complete", "reuse_record": record.model_dump(mode="json")}
            except Exception as exc:
                return {"status": "error", "message": f"reuse approval failed: {type(exc).__name__}: {exc}"}

        if task == "training.reuse.register":
            try:
                from Forge.core.reuse_registry import ReusableArtifact
                artifact = ReusableArtifact.model_validate(ctx.get("artifact", ctx))
                registered = self.reuse_registry.register_artifact(artifact)
                return {"status": "complete", "artifact": registered.model_dump(mode="json")}
            except Exception as exc:
                return {"status": "error", "message": f"artifact registration failed: {type(exc).__name__}: {exc}"}

        return {"status": "error", "message": f"Unknown task: {task}"}

    def _ds(self, ctx) -> Optional[Dataset]:
        if ctx.get("path"):
            try:
                return self.loader.load_csv(ctx["path"], ctx.get("target_column", ""),
                                          ctx.get("task_type", "classification"))
            except Exception:
                return None
        if ctx.get("X") and ctx.get("y"):
            return self.loader.load_arrays(ctx["X"], ctx["y"], ctx.get("feature_names"),
                                         ctx.get("task_type", "classification"),
                                         ctx.get("dataset_id", "in_memory"))
        return None

    def _drain_knowledge_outbox(self):
        try:
            if self.chronicle is None:
                log.error("training lesson is durably queued; no Chronicle client is configured")
                return
            result = self.knowledge_outbox.drain(self.chronicle, limit=25)
            if result["failed"]:
                log.error(
                    "training lessons remain queued for Chronicle retry: delivery=%s",
                    result,
                )
        except Exception:
            log.exception(
                "failed to deliver durable training lessons to Chronicle",
            )

    def _chronicle_operational_status(self) -> Dict[str, Any]:
        if self.chronicle is None:
            return {"ready": False, "configured": False, "error": "chronicle_not_configured"}
        status_method = getattr(self.chronicle, "get_status", None)
        if not callable(status_method):
            return {"ready": False, "configured": True, "error": "status_method_missing"}
        status = status_method()
        if not isinstance(status, dict):
            return {"ready": False, "configured": True, "error": "invalid_status_response"}
        store = status.get("store")
        ready = (
            status.get("running") is True
            and isinstance(store, dict)
            and isinstance(store.get("active"), int)
            and not isinstance(store.get("active"), bool)
        )
        return {
            "ready": ready,
            "configured": True,
            "running": status.get("running") is True,
            "active_memories": store.get("active") if isinstance(store, dict) else None,
            **({} if ready else {"error": "chronicle_store_not_ready"}),
        }

    def _operational_snapshot(self) -> Dict[str, Any]:
        """Return subsystem health and capability-specific readiness blockers."""
        errors = {}

        def collect(name, callback):
            try:
                return callback()
            except Exception as exc:
                errors[name] = type(exc).__name__
                return {"ready": False, "error": type(exc).__name__}

        champions = collect("champions", self.champion_registry.list_champions)
        champion_ownership = collect(
            "champion_ownership", self.champion_registry.ownership_status
        )
        experiment_ledger = collect("experiment_ledger", self.experiment_runner.stats)
        challenge_ledger = collect("challenge_ledger", self.challenger_engine.operational_status)
        promotion_ledger = collect("promotion_ledger", self.champion_registry.promotion_stats)
        model_registry = collect("model_registry", self.registry.stats)
        drift = collect("drift", self.drift_orchestrator.operational_status)
        deployment_stats = collect("deployment_stats", self.deployment_controller.stats)
        deployment = collect("deployment", self.deployment_controller.operational_status)
        chronicle_memory = collect("chronicle_memory", self._chronicle_operational_status)
        knowledge_outbox = collect("knowledge_outbox", self.knowledge_outbox.stats)
        training_requests = collect("training_requests", self.request_handler.operational_status)
        reuse = collect("reuse_registry", self.reuse_registry.operational_status)
        oracle_adapter = self.experiment_runner.adapters.get(ExperimentDomain.ORACLE_STRATEGY)
        oracle_regression_ready = (
            oracle_adapter is not None
            and getattr(oracle_adapter, "supports_paired_statistical_evidence", False) is True
            and getattr(oracle_adapter, "promotion_evidence_ready", False) is True
            and bool(getattr(oracle_adapter, "paired_statistical_metrics", ()))
        )
        promotion_authorization_configured = self.promotion_gate.authorization_configured
        training_request_authorization_configured = (
            self.request_handler.authorization_configured
        )
        run_reconciliation_authorization_configured = (
            getattr(self.experiment_runner, "reconciliation_authorization_configured", False)
        )
        challenge_reconciliation_authorization_configured = (
            self.challenger_engine.reconciliation_authorization_configured
        )
        request_reconciliation_authorization_configured = (
            self.request_handler.reconciliation_authorization_configured
        )

        def ready(component):
            return isinstance(component, dict) and component.get("ready") is True

        initialized_champion_records = (
            [item for item in champions if item.current_tenure is not None]
            if isinstance(champions, list) else []
        )
        initialized_champions = len(initialized_champion_records)
        statistically_qualified_champions = sum(
            self._champion_has_bootstrap_evidence(item)
            for item in initialized_champion_records
        )
        training_ready = (
            ready(experiment_ledger)
            and ready(model_registry)
            and ready(chronicle_memory)
            and ready(knowledge_outbox)
            and ready(training_requests)
            and knowledge_outbox.get("pending", 0) == 0
        )
        risk_sensitive_training_requests_ready = (
            ready(training_requests) and training_request_authorization_configured
        )
        bootstrap_ready = (
            training_ready
            and ready(promotion_ledger)
            and promotion_authorization_configured
        )
        challenge_ready = (
            training_ready
            and ready(challenge_ledger)
            and ready(promotion_ledger)
            and statistically_qualified_champions > 0
        )
        evolution_ready = bootstrap_ready and challenge_ready and ready(drift)
        deployment_metrics_ready = (
            isinstance(deployment_stats, dict)
            and "error" not in deployment_stats
            and deployment_stats.get("audit_integrity", {}).get("intact") is True
        )
        deployment_ready = (
            ready(model_registry) and ready(deployment) and deployment_metrics_ready
        )
        run_reconciliation_ready = (
            ready(experiment_ledger) and run_reconciliation_authorization_configured
        )
        challenge_reconciliation_ready = (
            ready(challenge_ledger) and challenge_reconciliation_authorization_configured
        )
        request_reconciliation_ready = (
            ready(training_requests) and request_reconciliation_authorization_configured
        )
        blockers = []
        for name, component in (
            ("experiment_ledger", experiment_ledger),
            ("model_registry", model_registry),
            ("challenge_ledger", challenge_ledger),
            ("promotion_ledger", promotion_ledger),
            ("drift_ledger", drift),
            ("deployment_controller", deployment),
            ("chronicle_memory", chronicle_memory),
            ("knowledge_outbox", knowledge_outbox),
            ("training_requests", training_requests),
        ):
            if not ready(component):
                blockers.append(f"{name}_not_ready")
        if not promotion_authorization_configured:
            blockers.append("promotion_authorization_not_configured")
        if not run_reconciliation_authorization_configured:
            blockers.append("run_reconciliation_authorization_not_configured")
        if not challenge_reconciliation_authorization_configured:
            blockers.append("challenge_reconciliation_authorization_not_configured")
        if not request_reconciliation_authorization_configured:
            blockers.append("training_request_reconciliation_authorization_not_configured")
        if not ready(reuse):
            blockers.append("reuse_registry_not_ready")
        elif not reuse.get("governed_reuse_ready", False):
            blockers.append("reuse_compatibility_or_authorization_not_configured")
        if not ready(champion_ownership):
            blockers.append("champion_ownership_not_ready")
        if isinstance(experiment_ledger, dict) and not experiment_ledger.get(
            "capacity_policy_ready", False
        ):
            blockers.append("experiment_capacity_policy_not_ready")
        if initialized_champions == 0:
            blockers.append("no_initialized_champion_positions")
        elif statistically_qualified_champions == 0:
            blockers.append("no_champion_with_current_statistical_evidence")
        if not deployment_metrics_ready:
            blockers.append("deployment_observability_not_ready")
        if not oracle_regression_ready:
            blockers.append("oracle_paired_regression_evidence_not_ready")
        if isinstance(knowledge_outbox, dict) and knowledge_outbox.get("pending", 0) > 0:
            blockers.append("chronicle_knowledge_outbox_pending")
        if isinstance(deployment, dict):
            if not deployment.get("authorization_verifier_configured", False):
                blockers.append("deployment_authorization_not_configured")
            if not deployment.get("health_gate_configured", False):
                blockers.append("deployment_health_gate_not_configured")
            elif not deployment.get("health_gate_ready", False):
                blockers.append("deployment_health_gate_unhealthy")

        return {
            "registered_experiment_domains": sorted(
                domain.value for domain in self.experiment_runner.adapters
            ),
            "experiment_adapter_capabilities": {
                domain.value: {
                    "adapter": type(adapter).__name__,
                    "paired_statistical_evidence": (
                        getattr(adapter, "supports_paired_statistical_evidence", False) is True
                    ),
                    "promotion_evidence_ready": (
                        getattr(adapter, "promotion_evidence_ready", False) is True
                    ),
                    "paired_statistical_metrics": list(
                        getattr(adapter, "paired_statistical_metrics", ())
                    ),
                }
                for domain, adapter in self.experiment_runner.adapters.items()
            },
            "champions": [
                {
                    "champion_id": item.champion_id,
                    "domain": item.domain,
                    "context_key": item.context_key,
                    "owner_agent": item.owner_agent,
                    "current_candidate_id": item.current_candidate_id,
                    "revision": item.revision,
                    "statistical_evidence_ready": self._champion_has_bootstrap_evidence(item),
                }
                for item in champions
            ] if isinstance(champions, list) else [],
            "champion_ownership": champion_ownership,
            "experiment_ledger": experiment_ledger,
            "challenge_ledger": challenge_ledger,
            "promotion_ledger": promotion_ledger,
            "model_registry": model_registry,
            "promotion_authorization_configured": promotion_authorization_configured,
            "training_request_authorization_configured": (
                training_request_authorization_configured
            ),
            "run_reconciliation_authorization_configured": (
                run_reconciliation_authorization_configured
            ),
            "challenge_reconciliation_authorization_configured": (
                challenge_reconciliation_authorization_configured
            ),
            "request_reconciliation_authorization_configured": (
                request_reconciliation_authorization_configured
            ),
            "drift": drift,
            "deployment_stats": deployment_stats,
            "deployment": deployment,
            "chronicle_memory": chronicle_memory,
            "knowledge_outbox": knowledge_outbox,
            "training_requests": training_requests,
            "reuse_registry": reuse,
            "operational_readiness": {
                "platform_ready": (
                    training_ready
                    and evolution_ready
                    and deployment_ready
                    and ready(reuse)
                    and reuse.get("governed_reuse_ready", False)
                    and ready(champion_ownership)
                    and oracle_regression_ready
                    and run_reconciliation_ready
                    and challenge_reconciliation_ready
                    and request_reconciliation_ready
                ),
                "training_ready": training_ready,
                "risk_sensitive_training_requests_ready": (
                    risk_sensitive_training_requests_ready
                ),
                "run_reconciliation_ready": run_reconciliation_ready,
                "challenge_reconciliation_ready": challenge_reconciliation_ready,
                "request_reconciliation_ready": request_reconciliation_ready,
                "bootstrap_ready": bootstrap_ready,
                "challenge_ready": challenge_ready,
                "evolution_ready": evolution_ready,
                "deployment_ready": deployment_ready,
                "oracle_regression_ready": oracle_regression_ready,
                "initialized_champions": initialized_champions,
                "statistically_qualified_champions": statistically_qualified_champions,
                "blockers": blockers,
                "component_errors": errors,
            },
        }

    @staticmethod
    def _champion_has_bootstrap_evidence(champion) -> bool:
        """Check that the incumbent carries evidence required for safe challenges."""
        tenure = getattr(champion, "current_tenure", None)
        if tenure is None:
            return False
        evidence = tenure.evidence_reference
        metrics = evidence.get("benchmark_metrics") or evidence.get("founding_metrics")
        draws = evidence.get("metric_bootstrap")
        metadata = evidence.get("bootstrap_evidence")
        if not isinstance(metrics, dict) or not metrics or not isinstance(draws, dict) or not draws:
            return False
        if not isinstance(metadata, dict):
            return False
        replicates = metadata.get("replicates")
        sample_count = metadata.get("sample_count")
        fingerprint = metadata.get("dataset_fingerprint")
        if (
            metadata.get("method") not in PAIRED_BOOTSTRAP_METHODS
            or not isinstance(replicates, int) or isinstance(replicates, bool)
            or not 200 <= replicates <= 20000
            or not isinstance(sample_count, int) or isinstance(sample_count, bool) or sample_count < 30
            or not isinstance(metadata.get("random_seed"), int)
            or isinstance(metadata.get("random_seed"), bool)
            or not 0 <= metadata.get("random_seed", -1) <= 2**32 - 1
            or not isinstance(fingerprint, str) or len(fingerprint) != 64
        ):
            return False
        if not all(char in "0123456789abcdef" for char in fingerprint.lower()):
            return False
        if getattr(champion, "domain", None) == "ORACLE_STRATEGY":
            partition = metadata.get("evaluation_partition")
            if not isinstance(partition, dict):
                return False
            partition_fingerprints = (
                partition.get("development_prefix_fingerprint"),
                partition.get("evaluation_suffix_fingerprint"),
            )
            start_index = partition.get("start_index")
            if (
                partition.get("method") != "chronological_suffix_holdout_v1"
                or not isinstance(start_index, int)
                or isinstance(start_index, bool)
                or start_index < 40
                or any(
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(char not in "0123456789abcdef" for char in value.lower())
                    for value in partition_fingerprints
                )
            ):
                return False
        elif getattr(champion, "domain", None) == "ML_SUPERVISED":
            partition = metadata.get("evaluation_partition")
            if not isinstance(partition, dict):
                return False
            fingerprints = (
                partition.get("train_indices_fingerprint"),
                partition.get("validation_indices_fingerprint"),
            )
            sample_count = partition.get("sample_count")
            validation_count = partition.get("validation_sample_count")
            partition_seed = partition.get("random_seed")
            if (
                partition.get("method") not in {
                    "sklearn_stratified_random_holdout_v1",
                    "sklearn_random_holdout_v1",
                    "python_seeded_random_holdout_v1",
                    "sklearn_stratified_group_holdout_v1",
                    "sklearn_group_holdout_v1",
                    "python_seeded_group_holdout_v1",
                }
                or not isinstance(partition_seed, int)
                or isinstance(partition_seed, bool)
                or partition_seed != metadata.get("random_seed")
                or not isinstance(sample_count, int)
                or isinstance(sample_count, bool)
                or sample_count < 30
                or not isinstance(validation_count, int)
                or isinstance(validation_count, bool)
                or not 30 <= validation_count < sample_count
                or any(
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(char not in "0123456789abcdef" for char in value.lower())
                    for value in fingerprints
                )
            ):
                return False
        for metric_name, metric_value in metrics.items():
            if (
                not isinstance(metric_name, str)
                or not isinstance(metric_value, (int, float))
                or isinstance(metric_value, bool)
                or not math.isfinite(metric_value)
            ):
                return False
            metric_draws = draws.get(metric_name)
            if (
                not isinstance(metric_draws, list)
                or len(metric_draws) != replicates
                or any(
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                    for value in metric_draws
                )
            ):
                return False
        return True

    def get_status(self) -> Dict[str, Any]:
        base = super().get_status() if _HAS_SHARED else {"name": self.name}
        base["backends"] = {n: b.describe() for n, b in self._backends.items()}
        evolution = self.execute("evolution.status", {})
        base["registry"] = evolution.get(
            "model_registry", {"ready": False, "error": "status_unavailable"}
        )
        try:
            base["method_discovery"] = self.discovery.stats()
        except Exception as exc:
            base["method_discovery"] = {"ready": False, "error": type(exc).__name__}
        base["drift"] = evolution.get(
            "drift", {"ready": False, "error": "status_unavailable"}
        )
        base["evolution"] = evolution
        return base


def _parse_datetime(value):
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError("observation window timestamps must be ISO strings or datetime objects")


class _ExtraModelWrapper:
    """Persistable wrapper so xgboost/lightgbm/svm models predict uniformly + scale inputs."""
    def __init__(self, backend, raw, task_type, scaler):
        self.backend = backend; self.raw = raw; self.task_type = task_type; self.scaler = scaler or {}
    def predict(self, X):
        m, s = self.scaler.get("means"), self.scaler.get("stds")
        Xs = ([[(r[j] - m[j]) / s[j] for j in range(len(r))] for r in X] if m and s else X)
        return predict_with(self.backend, self.raw, Xs)
