"""
shared.startup
==============
B-11/B-12 fix: Single canonical home for the two utilities that were
copy-pasted verbatim into every agent main.py:

  * CONFLICTING_MODULES  — list of top-level package names that collide
                           across repos when loaded in the same process.
  * unload_conflicting_modules() — purges those names from sys.modules so
                                   the next agent's bare `from core.X import Y`
                                   resolves against its own directory.
  * load_dotenv_early()  — loads the root .env before any shared.config
                           singleton is constructed (so API keys are present
                           at import time).

Usage in each agent main.py:
    from shared.startup import load_dotenv_early, unload_conflicting_modules
    load_dotenv_early()          # MUST be first line in main()
    ...
    unload_conflicting_modules() # call between each peer-agent load
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# The canonical list of top-level directory names that every repo ships and
# that therefore collide when multiple repos are loaded in the same process.
# ---------------------------------------------------------------------------
CONFLICTING_MODULES: list[str] = [
    "core", "agents", "intelligence", "memory", "research", "models", "training",
    "optimization", "communication", "infrastructure", "security", "api", "interfaces",
    "dashboard", "testing", "benchmarks", "simulations", "datasets", "documentation",
    "configs", "logs", "deployment", "plugins", "prompts", "tools", "constitutional",
    "execution", "registry",
]


def unload_conflicting_modules() -> None:
    """
    Forcibly remove stale top-level module entries from sys.modules so that
    the next `from core.X import Y` (or any other conflicting name) resolves
    against the correct repo root rather than whichever repo happened to be
    imported first.

    Call this between every peer-agent load in main.py.
    """
    to_delete: list[str] = []
    for mod in list(sys.modules.keys()):
        if mod in CONFLICTING_MODULES or any(
            mod.startswith(f"{m}.") for m in CONFLICTING_MODULES
        ):
            to_delete.append(mod)
    for mod in to_delete:
        sys.modules.pop(mod, None)


def load_dotenv_early(caller_file: str | None = None) -> None:
    """
    Load the project-root .env file BEFORE any agent or shared.config
    singleton is constructed.

    shared.config reads env vars at import time via a frozen dataclass.
    If dotenv has not been called yet, keys like NEWSAPI_KEY, GEMINI_API_KEY,
    MT5_LOGIN etc. are empty strings for the entire process lifetime because
    the singleton is never re-read.

    Pass __file__ from the calling main.py so we can walk up to the root:
        load_dotenv_early(__file__)

    Falls back to a manual line-by-line parser if python-dotenv is not
    installed, so this works in a bare environment.
    """
    # Determine the ecosystem root (the directory that contains all agent repos)
    if caller_file:
        start = Path(caller_file).resolve().parent
    else:
        start = Path(__file__).resolve().parent.parent  # shared/ -> root

    # Walk up looking for .env (stops at filesystem root)
    candidate = start
    env_path: Path | None = None
    for _ in range(5):  # max 5 levels up
        p = candidate / ".env"
        if p.exists():
            env_path = p
            break
        candidate = candidate.parent

    if env_path is None:
        return  # No .env found — env vars must be set externally

    log = logging.getLogger("ecosystem.startup")

    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(dotenv_path=str(env_path), override=False)
        log.info("dotenv loaded from %s", env_path)
        return
    except ImportError:
        pass  # python-dotenv not installed — use manual parser below

    # Manual fallback: parse key=value lines, skip comments and blanks
    try:
        with open(env_path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
        log.info("dotenv (manual) loaded from %s", env_path)
    except Exception as exc:
        log.warning("Could not load .env from %s: %s", env_path, exc)
