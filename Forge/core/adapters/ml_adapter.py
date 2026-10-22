"""
Forge V2 - ML Experiment Adapter (Phase 2)
==========================================
Wraps the existing V1 ForgeAgent training pipeline to execute controlled
A/B experiments defined by ExperimentContract.

Phase 2 Rules:
- dataset_path is REQUIRED. dataset_id resolution is deferred.
- Explicit hyperparameters CANNOT be combined with optimize=True.
- Adapter executes and collects evidence; it does NOT evaluate results.
- Model registration uses deterministic lineage: exp_<experiment_id>_<run_id>_<role>
"""

from typing import Any, Dict

from Forge.core.experimentation import (
    ExperimentAdapter,
    ExperimentContract,
    ExperimentDomain,
    ExperimentResult,
    ExperimentRun,
    ExperimentStatus,
    fingerprint_dataset_file,
)


class MLExperimentAdapter:
    """
    Executes ML experiments by calling the V1 ForgeAgent training pipeline twice:
    once for the baseline configuration and once for the treatment configuration.

    This adapter preserves ALL V1 safeguards (validation, leakage guard,
    cross-validation, registration gates) by delegating entirely to V1.
    """
    domain = ExperimentDomain.ML_SUPERVISED

    def __init__(self, forge_agent):
        """
        Args:
            forge_agent: The V1 ForgeAgent instance. Must support .act("training.run", ...)
        """
        self.forge_agent = forge_agent

    def execute(self, contract: ExperimentContract, run: ExperimentRun) -> ExperimentResult:
        """
        Executes the baseline and treatment training runs and packages the evidence.
        """
        # 1. Validate and resolve dataset
        dataset_path = self._resolve_dataset(contract)
        target_column = contract.data.target_column
        task_type = contract.variables.get("task_type", "classification")
        random_seed = contract.environment.random_seed
        if not target_column or task_type not in {"classification", "regression"}:
            raise ValueError("ML experiment requires a target column and classification/regression task_type")

        # 2. Validate experiment configurations
        self._validate_config(contract.baseline_config, "baseline")
        self._validate_config(contract.treatment_config, "treatment")

        # 3. Execute Baseline
        baseline_register_name = f"exp_{contract.experiment_id}_{run.run_id}_baseline"
        baseline_args = self._build_training_args(
            contract.baseline_config, dataset_path, target_column, baseline_register_name,
            task_type, random_seed, contract.evaluation.bootstrap_replicates,
        )
        baseline_result = self.forge_agent.act("training.run", baseline_args)

        # Check for V1 errors
        if baseline_result.get("status") == "error":
            return ExperimentResult(
                run_id=run.run_id,
                experiment_id=contract.experiment_id,
                status=ExperimentStatus.FAILED,
                error_message=f"Baseline training failed: {baseline_result.get('message', 'Unknown error')}",
                evidence={"baseline_full_result": baseline_result}
            )

        # 4. Execute Treatment
        treatment_register_name = f"exp_{contract.experiment_id}_{run.run_id}_treatment"
        self._resolve_dataset(contract)
        treatment_args = self._build_training_args(
            contract.treatment_config, dataset_path, target_column, treatment_register_name,
            task_type, random_seed, contract.evaluation.bootstrap_replicates,
        )
        treatment_result = self.forge_agent.act("training.run", treatment_args)

        # Check for V1 errors
        if treatment_result.get("status") == "error":
            return ExperimentResult(
                run_id=run.run_id,
                experiment_id=contract.experiment_id,
                status=ExperimentStatus.FAILED,
                error_message=f"Treatment training failed: {treatment_result.get('message', 'Unknown error')}",
                evidence={
                    "baseline_full_result": baseline_result,
                    "treatment_full_result": treatment_result
                }
            )

        baseline_bootstrap = baseline_result.get("evaluation_bootstrap")
        treatment_bootstrap = treatment_result.get("evaluation_bootstrap")
        baseline_partition = baseline_result.get("evaluation_partition")
        treatment_partition = treatment_result.get("evaluation_partition")
        valid_partition = self._valid_partition(
            baseline_partition, dataset_fingerprint=contract.data.data_fingerprint,
            random_seed=random_seed,
        )
        if (
            not isinstance(baseline_bootstrap, dict)
            or not isinstance(treatment_bootstrap, dict)
            or baseline_bootstrap.get("method") != "paired_percentile_bootstrap_v1"
            or treatment_bootstrap.get("method") != "paired_percentile_bootstrap_v1"
            or baseline_bootstrap.get("sample_count") != treatment_bootstrap.get("sample_count")
            or baseline_bootstrap.get("replicates") != treatment_bootstrap.get("replicates")
            or baseline_bootstrap.get("sample_count") != (
                baseline_partition.get("validation_sample_count")
                if isinstance(baseline_partition, dict) else None
            )
            or not valid_partition
            or treatment_partition != baseline_partition
        ):
            return ExperimentResult(
                run_id=run.run_id,
                experiment_id=contract.experiment_id,
                status=ExperimentStatus.FAILED,
                error_message="Baseline and treatment did not produce matching bootstrap evidence",
                evidence={
                    "baseline_model_id": baseline_result.get("model_id"),
                    "treatment_model_id": treatment_result.get("model_id"),
                },
            )

        # 5. Package results (Execution & Evidence, NOT Evaluation)
        self._resolve_dataset(contract)
        result = ExperimentResult(
            run_id=run.run_id,
            experiment_id=contract.experiment_id,
            status=ExperimentStatus.COMPLETED,
            metrics={
                "baseline": baseline_result.get("metrics", {}),
                "treatment": treatment_result.get("metrics", {}),
                "bootstrap": {
                    "method": "paired_percentile_bootstrap_v1",
                    "replicates": baseline_bootstrap["replicates"],
                    "sample_count": baseline_bootstrap["sample_count"],
                    "baseline": baseline_bootstrap["metrics"],
                    "treatment": treatment_bootstrap["metrics"],
                },
            },
            evidence={
                "baseline_full_result": self._without_bootstrap(baseline_result),
                "treatment_full_result": self._without_bootstrap(treatment_result),
                "baseline_model_id": baseline_result.get("model_id"),
                "treatment_model_id": treatment_result.get("model_id"),
                "baseline_experiment_id": baseline_result.get("experiment_id"),
                "treatment_experiment_id": treatment_result.get("experiment_id"),
                "baseline_chosen_backend": baseline_result.get("chosen_backend"),
                "treatment_chosen_backend": treatment_result.get("chosen_backend"),
                "data_quality_baseline": baseline_result.get("data_quality"),
                "data_quality_treatment": treatment_result.get("data_quality"),
                "dataset_fingerprint": contract.data.data_fingerprint,
                "configuration_fingerprint": contract.environment.configuration_fingerprint,
                "random_seed": random_seed,
                "environment_fingerprint": run.environment_fingerprint,
                "environment_snapshot": run.environment_snapshot,
                "evaluation_partition": baseline_partition,
            }
        )
        for trained_result in (baseline_result, treatment_result):
            model_id = trained_result.get("model_id")
            if not model_id:
                raise ValueError("Training completed without a registered model artifact")
            self.forge_agent.registry.attach_source_lineage(
                model_id=model_id,
                source_experiment_id=contract.experiment_id,
                source_run_id=run.run_id,
                source_result_id=result.result_id,
                data_fingerprint=contract.data.data_fingerprint,
            )
        return result

    @staticmethod
    def _without_bootstrap(training_result: Dict[str, Any]) -> Dict[str, Any]:
        """Keep aggregate training evidence while avoiding duplicate bootstrap payloads."""
        return {key: value for key, value in training_result.items() if key != "evaluation_bootstrap"}

    @staticmethod
    def _valid_partition(
        partition: Any, *, dataset_fingerprint: str, random_seed: int
    ) -> bool:
        if not isinstance(partition, dict):
            return False
        fingerprints = (
            partition.get("train_indices_fingerprint"),
            partition.get("validation_indices_fingerprint"),
            partition.get("holdout_fingerprint"),
        )
        sample_count = partition.get("sample_count")
        validation_count = partition.get("validation_sample_count")
        partition_seed = partition.get("random_seed")
        return (
            partition.get("method") in {
                "sklearn_stratified_random_holdout_v1",
                "sklearn_random_holdout_v1",
                "python_seeded_random_holdout_v1",
                "sklearn_stratified_group_holdout_v1",
                "sklearn_group_holdout_v1",
                "python_seeded_group_holdout_v1",
            }
            and isinstance(partition_seed, int)
            and not isinstance(partition_seed, bool)
            and partition_seed == random_seed
            and isinstance(sample_count, int) and not isinstance(sample_count, bool)
            and sample_count >= 30
            and isinstance(validation_count, int) and not isinstance(validation_count, bool)
            and 30 <= validation_count < sample_count
            and isinstance(dataset_fingerprint, str)
            and len(dataset_fingerprint) == 64
            and all(char in "0123456789abcdef" for char in dataset_fingerprint.lower())
            and all(
                isinstance(value, str)
                and len(value) == 64
                and all(char in "0123456789abcdef" for char in value.lower())
                for value in fingerprints
            )
        )

    def _resolve_dataset(self, contract: ExperimentContract) -> str:
        """
        Phase 2: dataset_path is REQUIRED. dataset_id resolution is deferred.
        """
        if contract.data.dataset_path:
            canonical_path, fingerprint = fingerprint_dataset_file(contract.data.dataset_path)
            if not contract.data.data_fingerprint:
                raise ValueError("ML experiment contract lacks a persisted dataset fingerprint")
            if fingerprint != contract.data.data_fingerprint:
                raise ValueError("Dataset contents changed after the experiment was submitted")
            return canonical_path

        if contract.data.dataset_id:
            raise NotImplementedError(
                "MLExperimentAdapter Phase 2 currently requires dataset_path; "
                f"dataset_id resolution (dataset_id={contract.data.dataset_id}) is deferred to a future phase."
            )

        raise ValueError(
            "MLExperimentAdapter requires either dataset_path or dataset_id in contract.data. "
            "Neither was provided."
        )

    def _validate_config(self, config: Dict[str, Any], role: str) -> None:
        """
        Validates the experiment configuration for consistency.

        Phase 2 Rule: Explicit hyperparameters cannot be combined with automatic optimization.
        """
        has_hyperparameters = "hyperparameters" in config and config["hyperparameters"] is not None
        optimize_flag = config.get("optimize", False)

        if has_hyperparameters and optimize_flag:
            raise ValueError(
                f"Explicit hyperparameters in {role}_config cannot be combined "
                f"with automatic optimization (optimize=True) in Phase 2. "
                f"Set optimize=False when providing explicit hyperparameters."
            )

    def _build_training_args(
        self,
        config: Dict[str, Any],
        dataset_path: str,
        target_column: str,
        register_name: str,
        task_type: str,
        random_seed: int,
        bootstrap_replicates: int,
    ) -> Dict[str, Any]:
        """
        Builds the argument dictionary for ForgeAgent.act("training.run", ...).
        """
        args = {
            "path": dataset_path,
            "target_column": target_column,
            "register_as": register_name,
            "task_type": task_type,
            "random_seed": random_seed,
            "bootstrap_replicates": bootstrap_replicates,
            "include_evaluation_bootstrap": True,
        }

        # Optional backend override
        if "backend" in config:
            args["backend"] = config["backend"]

        # Optional hyperparameters (Phase 2 extension)
        if "hyperparameters" in config:
            args["hyperparameters"] = config["hyperparameters"]
            # Phase 2 rule: explicit hyperparameters imply optimize=False
            args["optimize"] = False
        elif "optimize" in config:
            args["optimize"] = config["optimize"]
        # If neither is specified, V1 defaults to optimize=True

        return args
