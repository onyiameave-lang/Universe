"""
shared.config
=============
Process-wide configuration. Every repository imports get_config() rather than
reading os.environ directly. (Book IV Ch VII.)
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import List, Optional


def _bool(name, default=False):
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "y", "on")

def _int(name, default):
    try: return int(os.getenv(name, str(default)))
    except (TypeError, ValueError): return default

def _float(name, default):
    try: return float(os.getenv(name, str(default)))
    except (TypeError, ValueError): return default

def _list(name, default=None, sep=","):
    raw = os.getenv(name)
    return list(default or []) if not raw else [s.strip() for s in raw.split(sep) if s.strip()]


@dataclass(frozen=True)
class EcosystemConfig:
    ecosystem_root: str = field(default_factory=lambda: os.getenv("ECOSYSTEM_ROOT", os.getcwd()))
    memory_path: str = field(default_factory=lambda: os.getenv("MEMORY_PATH", "memory_store"))
    logs_path: str = field(default_factory=lambda: os.getenv("LOGS_PATH", "logs"))
    models_path: str = field(default_factory=lambda: os.getenv("MODELS_PATH", "models"))
    protocol_version: str = "1.0.0"
    default_priority: int = 4
    default_ttl_ms: int = 5000
    enable_sandbox: bool = field(default_factory=lambda: _bool("ENABLE_SANDBOX", True))
    require_signed_messages: bool = field(default_factory=lambda: _bool("REQUIRE_SIGNED_MESSAGES", False))
    # RL / trading risk limits
    rl_max_positions: int = field(default_factory=lambda: _int("RL_MAX_POSITIONS", 1))
    rl_max_drawdown_pct: float = field(default_factory=lambda: _float("RL_MAX_DRAWDOWN_PCT", 0.20))
    rl_risk_per_trade: float = field(default_factory=lambda: _float("RL_RISK_PER_TRADE", 0.01))
    # memory
    memory_embedding_dim: int = field(default_factory=lambda: _int("MEMORY_EMBEDDING_DIM", 384))
    memory_use_real_embeddings: bool = field(default_factory=lambda: _bool("MEMORY_USE_REAL_EMBEDDINGS", False))
    # coordination
    coordinator_heartbeat_sec: int = field(default_factory=lambda: _int("COORDINATOR_HEARTBEAT_SEC", 5))
    # external sources
    enabled_news_sources: List[str] = field(default_factory=lambda: _list(
        "ENABLED_NEWS_SOURCES", ["rss", "newsapi", "gdelt", "hackernews"]))
    enabled_social_sources: List[str] = field(default_factory=lambda: _list(
        "ENABLED_SOCIAL_SOURCES", ["reddit", "hackernews", "stocktwits"]))
    # keys (passthrough; all optional)
    openai_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    anthropic_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    gemini_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    newsapi_key: str = field(default_factory=lambda: os.getenv("NEWSAPI_KEY", ""))
    mt5_login: str = field(default_factory=lambda: os.getenv("MT5_LOGIN", ""))
    mt5_password: str = field(default_factory=lambda: os.getenv("MT5_PASSWORD", ""))
    mt5_server: str = field(default_factory=lambda: os.getenv("MT5_SERVER", ""))
    oracle_paper_trading: bool = field(default_factory=lambda: _bool("ORACLE_PAPER_TRADING", True))


_config: Optional[EcosystemConfig] = None

def get_config(reload: bool = False) -> EcosystemConfig:
    global _config
    if _config is None or reload:
        _config = EcosystemConfig()
    return _config
