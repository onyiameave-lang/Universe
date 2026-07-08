"""
Aegis.core.constitution
=======================
The machine-readable constitution Aegis enforces. (Book I Part IV; Book II
Ch VII Engineering Rules; Book III Part II URS; Book IV Part II Repository
Standards; Book VI Human Constitution.)

Every rule is a concrete, checkable predicate over real manifests, code, and
actions. This is the single source of truth Aegis audits against, so its
verdicts are deterministic and explainable, never opinion.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List


# ---- Universal Repository Standard: mandatory directories (Book III/IV) ----
URS_REQUIRED_DIRECTORIES = [
    "core", "agents", "intelligence", "memory", "research", "models",
    "training", "optimization", "communication", "infrastructure", "security",
    "api", "interfaces", "dashboard", "testing", "benchmarks", "simulations",
    "datasets", "documentation", "configs", "logs", "deployment", "plugins",
    "prompts", "tools", "constitutional",
]

# ---- Repository manifest required fields (Book IV Part II Ch I) ----
MANIFEST_REQUIRED_FIELDS = [
    "constitutional_name", "former_name", "repository_id", "version",
    "constitution_version", "repository_type", "domain", "primary_mission",
    "capabilities", "channels", "security_level", "memory_namespace",
]

# ---- Agent required attributes (Book I Part IV Article III/IV/VI) ----
AGENT_REQUIRED_ATTRS = [
    "name", "repository", "domain", "capabilities", "channels",
    "mission", "memory_namespace", "security_level",
]

# ---- Engineering prohibitions (Book II Ch VII) ----
ENGINEERING_PROHIBITIONS = {
    "hardcoded_secrets": "No hardcoded knowledge / secrets.",
    "silent_failures": "No silent failures (bare/broad except: pass).",
    "circular_dependencies": "No circular dependencies.",
    "undocumented_apis": "No undocumented APIs.",
    "hidden_prompts": "No hidden prompts.",
    "unlogged_agent_creation": "No unlogged agent creation.",
    "duplicate_implementations": "No duplicate implementations.",
}

VALID_SECURITY_LEVELS = {"public", "standard", "elevated", "critical", "constitutional"}
VALID_LIFECYCLE = {"designing", "developing", "testing", "deployed",
                   "optimizing", "evolving", "legacy", "retired"}

# ---- Human sovereignty rules (Book VI Part I) ----
HUMAN_SOVEREIGNTY_RULES = [
    "human_override_available", "actions_auditable",
    "reasoning_explainable", "no_unbounded_autonomy",
]

# ---- Trust pillars (Book VI Part II Ch II) ----
TRUST_PILLARS = ["truth", "integrity", "competence", "transparency",
                 "consistency", "responsibility", "respect"]


@dataclass
class RuleResult:
    rule: str
    passed: bool
    detail: str = ""
    reference: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"rule": self.rule, "passed": self.passed,
                "detail": self.detail, "reference": self.reference}


SECRET_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*=\s*['\"][^'\"]{8,}['\"]"), "inline credential"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI-style key"),
    (re.compile(r"(?i)aws_secret_access_key\s*=\s*['\"][^'\"]+['\"]"), "AWS secret"),
    (re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----"), "private key block"),
]
SILENT_FAILURE_PATTERN = re.compile(r"except\s*:\s*\n\s*pass", re.MULTILINE)
BROAD_EXCEPT_PASS = re.compile(r"except\s+Exception\s*:\s*\n\s*pass", re.MULTILINE)


def check_manifest_fields(manifest: Dict[str, Any]) -> List[RuleResult]:
    results = []
    for field in MANIFEST_REQUIRED_FIELDS:
        present = bool(manifest.get(field))
        results.append(RuleResult(f"manifest.{field}", present,
                                 "present" if present else "missing required field",
                                 "Book IV Part II Ch I"))
    sec = manifest.get("security_level", "")
    results.append(RuleResult("manifest.security_level.valid", sec in VALID_SECURITY_LEVELS,
                             f"'{sec}'", "Book III Part II Ch I"))
    return results


def check_urs_directories(present_dirs: List[str]) -> List[RuleResult]:
    present = set(present_dirs)
    return [RuleResult(f"urs.dir.{d}", d in present,
                      "present" if d in present else "missing constitutional directory",
                      "Book III Part II Ch II") for d in URS_REQUIRED_DIRECTORIES]


def check_agent_attrs(agent_config: Dict[str, Any]) -> List[RuleResult]:
    return [RuleResult(f"agent.{attr}", bool(agent_config.get(attr)),
                      "declared" if agent_config.get(attr) else "missing",
                      "Book I Part IV Article III-VI") for attr in AGENT_REQUIRED_ATTRS]


def scan_source_for_prohibitions(source: str, filename: str = "") -> List[RuleResult]:
    results: List[RuleResult] = []
    secret_hits = [label for pattern, label in SECRET_PATTERNS if pattern.search(source)]
    results.append(RuleResult("engineering.no_hardcoded_secrets", not secret_hits,
                             "clean" if not secret_hits else f"found: {', '.join(secret_hits)}",
                             "Book II Ch VII"))
    silent = SILENT_FAILURE_PATTERN.search(source) or BROAD_EXCEPT_PASS.search(source)
    intentional = "# aegis:allow-silent" in source
    results.append(RuleResult("engineering.no_silent_failures", (not silent) or intentional,
                             "clean" if not silent else "bare/broad except: pass detected",
                             "Book II Ch VII"))
    return results


def check_human_sovereignty(capabilities: Dict[str, Any]) -> List[RuleResult]:
    return [RuleResult(f"human.{rule}", bool(capabilities.get(rule, False)),
                      "satisfied" if capabilities.get(rule) else "safeguard not present",
                      "Book VI Part I") for rule in HUMAN_SOVEREIGNTY_RULES]
