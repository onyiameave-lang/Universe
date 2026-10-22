"""
Forge V2 - Domain Adapters Package
===================================
Each adapter wraps a specific domain's execution logic and translates
ExperimentContracts into concrete execution steps.
"""

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

__all__ = [
    "MLExperimentAdapter",
    "OracleExperimentAdapter",
    "DiscoveryProfile",
    "DiscoveryResult",
    "DiscoveryTranslator",
    "PromptOptimizationAdapter",
    "WorkflowOptimizationAdapter",
    "ConfigOptimizationAdapter",
]
