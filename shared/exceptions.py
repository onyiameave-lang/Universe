"""
shared.exceptions
=================
Constitutional exception hierarchy. Silent failures are forbidden. (Book II Ch XI.)
"""
from __future__ import annotations
from typing import Any, Dict, Optional


class EcosystemError(Exception):
    def __init__(self, message: str, repository: str = "", agent: str = "",
                 context: Optional[Dict[str, Any]] = None, recoverable: bool = True):
        super().__init__(message)
        self.repository = repository
        self.agent = agent
        self.context = context or {}
        self.recoverable = recoverable

    def to_dict(self) -> Dict[str, Any]:
        return {"error_type": self.__class__.__name__, "message": str(self),
                "repository": self.repository, "agent": self.agent,
                "context": self.context, "recoverable": self.recoverable}


class ConstitutionalViolation(EcosystemError):
    def __init__(self, message: str, article: str = "", principle: str = "", **kw):
        super().__init__(message, recoverable=False, **kw)
        self.article = article
        self.principle = principle

    def to_dict(self):
        d = super().to_dict(); d["article"] = self.article; d["principle"] = self.principle
        return d


class ProtocolError(EcosystemError):
    def __init__(self, message: str, message_id: str = "", **kw):
        super().__init__(message, **kw); self.message_id = message_id


class RepositoryError(EcosystemError): pass
class MemoryError(EcosystemError): pass
class AgentError(EcosystemError): pass
class TrainingError(EcosystemError): pass
class ResearchError(EcosystemError): pass
class CreationError(EcosystemError): pass


class SecurityError(EcosystemError):
    def __init__(self, message: str, severity: str = "high", **kw):
        super().__init__(message, recoverable=False, **kw); self.severity = severity


class GovernanceError(EcosystemError):
    def __init__(self, message: str, decision_id: str = "", **kw):
        super().__init__(message, recoverable=False, **kw); self.decision_id = decision_id
