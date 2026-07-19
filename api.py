#!/usr/bin/env python3
"""
api.py - FastAPI interface for the Universal AI ecosystem.
Bridges the autonomous agent backend with the HTML frontends.
"""
import sys
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import time

# Add current dir to path for imports
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ecosystem

# Import agent interface routers
from Nexus.api.interface import router as nexus_router
from Oracle.api.interface import router as oracle_router
from Atlas.api.interface import router as atlas_router
from Pulse.api.interface import router as pulse_router
from Aegis.api.interface import router as aegis_router
from Sentinel.api.interface import router as sentinel_router
from Forge.api.interface import router as forge_router
from Genesis.api.interface import router as genesis_router
from Chronicle.api.interface import router as chronicle_router

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("api")

# Global state to hold agents
AGENTS: Dict[str, Any] = {}
LOGS: List[Dict[str, Any]] = []

def add_log(agent: str, type: str, msg: str):
    LOGS.append({
        "time": time.strftime("%H:%M:%S"),
        "agent": agent.upper(),
        "type": type.upper(),
        "msg": msg
    })
    if len(LOGS) > 100:
        LOGS.pop(0)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Boot the ecosystem agents on startup."""
    logger.info("Booting AI Ecosystem...")
    add_log("system", "boot", "Starting AI Ecosystem - civilizing the digital frontier")
    
    # Reusing the loading logic from ecosystem.py
    BOOT_ORDER = ["chronicle", "atlas", "nexus", "aegis", "sentinel", "pulse", "forge", "genesis", "oracle"]
    chronicle = None
    
    for name in BOOT_ORDER:
        ecosystem._unload_conflicting_modules()
        if name not in ecosystem.REPO_MAP: continue
        folder, rel, cls = ecosystem.REPO_MAP[name]
        C = ecosystem._load(folder, rel, cls)
        if not C:
            add_log("system", "error", f"Failed to load agent {name}")
            continue

        inst = None
        try:
            if name == "chronicle":
                inst = C(storage_dir=str(ROOT / "Chronicle" / "memory" / "store"))
                chronicle = inst
            elif name == "atlas":
                inst = C(chronicle_client=chronicle)
            elif name == "oracle":
                inst = C(
                    chronicle_client=chronicle,
                    atlas_client=AGENTS.get("atlas"),
                    sentinel_client=AGENTS.get("sentinel"),
                    pulse_client=AGENTS.get("pulse"),
                )
            else:
                try:
                    inst = C(chronicle_client=chronicle)
                except TypeError:
                    inst = C()
            
            if inst:
                AGENTS[name] = inst
                add_log(name, "ready", f"Agent {name} online and operational")
        except Exception as e:
            logger.error(f"Failed to load agent {name}: {e}")
            add_log(name, "error", f"Initialization failed: {str(e)}")
            
    yield
    # Shutdown logic — B-8 fix: call stop() on every agent so heartbeat threads
    # are joined and Chronicle's vector store is flushed to disk consistently.
    logger.info("Shutting down AI Ecosystem...")
    # Stop in reverse boot order so dependents shut down before their dependencies
    SHUTDOWN_ORDER = ["oracle", "genesis", "forge", "pulse", "sentinel", "aegis", "nexus", "atlas", "chronicle"]
    for name in SHUTDOWN_ORDER:
        agent = AGENTS.get(name)
        if agent is None:
            continue
        try:
            if hasattr(agent, "stop"):
                agent.stop()
                logger.info("Agent %s stopped cleanly", name)
        except Exception as exc:
            logger.warning("Error stopping agent %s: %s", name, exc)
    AGENTS.clear()

app = FastAPI(title="Universal AI API", lifespan=lifespan)

# Enable CORS for local HTML files and development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include distributed agent routers
app.include_router(nexus_router, prefix="/agents")
app.include_router(oracle_router, prefix="/agents")
app.include_router(atlas_router, prefix="/agents")
app.include_router(pulse_router, prefix="/agents")
app.include_router(aegis_router, prefix="/agents")
app.include_router(sentinel_router, prefix="/agents")
app.include_router(forge_router, prefix="/agents")
app.include_router(genesis_router, prefix="/agents")
app.include_router(chronicle_router, prefix="/agents")

# Serve the HTML dashboards from the same FastAPI process. This keeps the
# existing API routes intact while making http://localhost:8000 load the portal.
for static_name in [
    "shared",
    "Nexus",
    "Oracle",
    "Atlas",
    "Pulse",
    "Aegis",
    "Sentinel",
    "Forge",
    "Genesis",
    "Chronicle",
]:
    static_dir = ROOT / static_name
    if static_dir.exists():
        app.mount(f"/{static_name}", StaticFiles(directory=str(static_dir)), name=f"static-{static_name.lower()}")

@app.get("/")
async def root(request: Request):
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return FileResponse(ROOT / "index.html")
    return {
        "status": "online",
        "ecosystem": "Universal AI",
        "active_agents": list(AGENTS.keys()),
        "version": "1.0.0"
    }

@app.get("/api/status")
async def api_status():
    return {
        "status": "online",
        "ecosystem": "Universal AI",
        "active_agents": list(AGENTS.keys()),
        "version": "1.0.0"
    }

@app.get("/agents")
async def list_agents():
    """Return metadata for all active agents."""
    results = []
    for name, agent in AGENTS.items():
        results.append({
            "id": name,
            "name": getattr(agent, "name", name),
            "description": getattr(agent, "description", ""),
            "status": getattr(agent, "lifecycle_status", "active"),
            "capabilities": getattr(agent, "capabilities", [])
        })
    return results

@app.get("/logs")
async def get_logs(limit: int = 10):
    """Return the latest system logs."""
    return LOGS[-limit:]

@app.post("/agents/{name}/query")
async def query_agent(name: str, request: Request):
    """Send a command to an agent."""
    if name not in AGENTS:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    data = await request.json()
    prompt = data.get("prompt", "")
    
    add_log(name, "query", f"Processing: {prompt}")
    
    agent = AGENTS[name]
    try:
        if hasattr(agent, "act"):
            # B-6 fix: act() takes (task: str, context: dict) — never a bare string
            response = agent.act("user.query", {"query": prompt, "_sender": "api"})
        elif hasattr(agent, "execute"):
            response = agent.execute("user.query", {"query": prompt})
        else:
            response = f"Awaiting instructions. {name} is listening."
            
        add_log(name, "response", "Query completed successfully")
        return {"response": response}
    except Exception as e:
        add_log(name, "error", str(e))
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)