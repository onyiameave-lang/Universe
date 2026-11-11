"""
Forge V2 - Domain Adapters Package
===================================
Each adapter wraps a specific domain's execution logic and translates
ExperimentContracts into concrete execution steps.
"""

from Forge.core.adapters.universal_agent_adapters import (
    AegisTrainingAdapter,
    AtlasTrainingAdapter,
    BaseUniverseAgentAdapter,
    ChronicleTrainingAdapter,
    GenesisTrainingAdapter,
    OracleTrainingAdapter,
    OracleV1CompatibilityAdapter,
    SentinelTrainingAdapter,
    UniversalAgentTrainingAdapter,
    get_universal_agent_adapter_registry,
)

try:
    from Forge.core.adapters.ml_adapter import MLExperimentAdapter
    from Forge.core.adapters.oracle_adapter import OracleExperimentAdapter
    from Forge.core.adapters.discovery_translator import (
        DiscoveryProfile,
        DiscoveryResult,
        DiscoveryTranslator,
    )
    from Forge.core.adapters.prompt_adapter import PromptOptimizationAdapter
    from Forge.core.adapters.workflow_adapter import WorkflowOptimizationAdapter
    from Forge.core.adapters.config_adapter import ConfigOptimizationAdapter
except ImportError:  # pragma: no cover - optional pydantic dependency
    MLExperimentAdapter = None  # type: ignore[assignment,misc]
    OracleExperimentAdapter = None  # type: ignore[assignment,misc]
    DiscoveryProfile = None  # type: ignore[assignment,misc]
    DiscoveryResult = None  # type: ignore[assignment,misc]
    DiscoveryTranslator = None  # type: ignore[assignment,misc]
    PromptOptimizationAdapter = None  # type: ignore[assignment,misc]
    WorkflowOptimizationAdapter = None  # type: ignore[assignment,misc]
    ConfigOptimizationAdapter = None  # type: ignore[assignment,misc]

__all__ = [
    "MLExperimentAdapter",
    "OracleExperimentAdapter",
    "DiscoveryProfile",
    "DiscoveryResult",
    "DiscoveryTranslator",
    "PromptOptimizationAdapter",
    "WorkflowOptimizationAdapter",
    "ConfigOptimizationAdapter",
    "UniversalAgentTrainingAdapter",
    "BaseUniverseAgentAdapter",
    "OracleTrainingAdapter",
    "AtlasTrainingAdapter",
    "ChronicleTrainingAdapter",
    "SentinelTrainingAdapter",
    "GenesisTrainingAdapter",
    "AegisTrainingAdapter",
    "OracleV1CompatibilityAdapter",
    "get_universal_agent_adapter_registry",
]

