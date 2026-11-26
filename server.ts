import express from "express";
import path from "path";
import fs from "fs";
import { spawn } from "child_process";
import { fileURLToPath } from "url";
import { GoogleGenAI, ThinkingLevel, Type } from "@google/genai";
import dotenv from "dotenv";

import {
  loadAgentManifests,
  CONSTITUTIONAL_PRINCIPLES,
  ENGINEERING_RULES,
  CONSTITUTIONAL_OATH,
  AgentManifest,
} from "./src/server/constitution.ts";
import {
  ChronicleAuthoritativeStore,
  ChronicleMemoryEntry,
  LearningEpisode,
} from "./src/server/chronicleStore.ts";
import {
  UniversalForgeCore,
  CandidateLifecycleState,
} from "./src/server/forgeCore.ts";
import {
  OracleTrainingAdapter,
  PilotCandidateCode,
} from "./src/server/oracleAdapter.ts";
import { TraderStyleCode } from "./src/server/mtfDataEngine.ts";

dotenv.config();

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

function safeIsoTimestamp(unixSec?: number | null): string {
  const ts =
    typeof unixSec === "number" && Number.isFinite(unixSec) && unixSec > 0
      ? unixSec * 1000
      : Date.now();
  const d = new Date(ts);
  return Number.isNaN(d.getTime())
    ? new Date().toISOString()
    : d.toISOString();
}

function safeUtcTime(unixSec?: number | null): string {
  return safeIsoTimestamp(unixSec).slice(11, 19) + " UTC";
}

const app = express();
app.use(express.json({ limit: "5mb" }));

// ── 1. Initialize Constitutional Manifests, Chronicle Hard Drive & Forge Core ─
const AGENT_MANIFESTS: Record<string, AgentManifest> =
  loadAgentManifests(__dirname);
const chronicle = new ChronicleAuthoritativeStore(__dirname);
const forgeCore = new UniversalForgeCore(chronicle);
const oracleAdapter = new OracleTrainingAdapter(
  __dirname,
  chronicle,
  forgeCore
);

// Bootstrap 7-Timeframe (W1..M5) + Session + Regime + Atlas Discovery for GBPUSD & EURUSD (all 4 archetypes)
async function bootstrapOracleV2MultiTimeframeDiscovery() {
  try {
    const existingV2 = forgeCore
      .listCandidateArtifacts()
      .filter((c) => c.candidate_code.startsWith("V2_"));
    const hasRangeSpecialist = existingV2.some(
      (c) => c.candidate_code === "V2_RANGE_SPECIALIST"
    );
    const hasMonteCarlo = existingV2.some(
      (c) => Boolean(c.monte_carlo_stress_report)
    );
    if (existingV2.length < 8 || !hasRangeSpecialist || !hasMonteCarlo) {
      console.log(
        "[Forge V2 + Atlas] Ingesting 7-timeframe (W1..M5) intraday data & training all 4 Trader Archetypes (Scalping, Day-Trading, Swing-Trading, Range Specialist) with 1,000-path Monte Carlo & Instrument Physics DNA for Oracle V2..."
      );
      await oracleAdapter.trainAllOracleV2Styles({ symbol: "GBPUSD" });
      await oracleAdapter.trainAllOracleV2Styles({ symbol: "EURUSD" });
      oracleAdapter.runFullFiveCandidatePilot({
        symbol: "GBPUSD",
        regime: "trending_up",
      });
      console.log(
        "[Forge V2 + Atlas] Completed 7-Timeframe Oracle V2 discovery bootstrap across all 4 archetypes."
      );
    }
  } catch (err) {
    console.error("[Forge V2 + Atlas] Bootstrap warning:", err);
  }
}
bootstrapOracleV2MultiTimeframeDiscovery();

function getGenAI(): GoogleGenAI | null {
  const key = process.env.GEMINI_API_KEY || process.env.NEXUS_GEMINI_KEY;
  if (!key || key === "MY_GEMINI_API_KEY") return null;
  return new GoogleGenAI({ apiKey: key });
}

function readJsonFileSafe(relPath: string, fallback: any = {}): any {
  try {
    const full = path.join(__dirname, relPath);
    if (fs.existsSync(full)) {
      return JSON.parse(fs.readFileSync(full, "utf8"));
    }
  } catch {
    // Return fallback
  }
  return fallback;
}

function buildStorageTopology() {
  const overview = chronicle.getChronicleOverview();
  const agentRamList = Object.keys(AGENT_MANIFESTS).map((id) => {
    const snap = chronicle.getAgentRamSnapshot(id);
    let outboxPending = 0;
    if (id === "oracle") {
      const outboxPath = path.join(
        __dirname,
        "Oracle",
        "memory",
        "forge_bridge_outbox.jsonl"
      );
      if (fs.existsSync(outboxPath)) {
        outboxPending = fs
          .readFileSync(outboxPath, "utf8")
          .split("\n")
          .filter((l) => l.trim().length > 0).length;
      }
    }
    return {
      agent: id,
      ram_files: snap.local_ram_files,
      outbox_pending_events: outboxPending,
      episodes_in_chronicle: snap.total_episodes_in_chronicle,
      lessons_learned: snap.lessons_learned,
    };
  });

  return {
    chronicle_hard_drive: {
      state_keys_count: overview.state_keys_count,
      total_revisions:
        overview.state_keys_count +
        overview.memories_count +
        overview.episodes_count,
      memories_count: overview.memories_count,
      episodes_count: overview.episodes_count,
      lessons_count: overview.lessons_count,
      recurrences_consolidated: overview.recurrences_consolidated,
      storage_formatted: overview.storage_formatted,
    },
    agent_ram_and_outboxes: agentRamList,
  };
}

// ── 2. Dynamic Agent Data Builders (Zero Hardcoded Constants) ────────────────
const PYTHON_BIN =
  process.env.PYTHON_BIN ||
  (process.platform === "win32" ? "python" : "python3");

let _lastFeedRefreshMs = 0;
app.get("/health", (_req, res) => {
  const storage = buildStorageTopology();
  res.json({
    status: "healthy",
    service: "universe-forge-v2-gateway",
    timestamp: new Date().toISOString(),
    registered_agents: Object.keys(AGENT_MANIFESTS).length,
    agent_runtime_health: "not_probed",
    chronicle_state_keys: storage.chronicle_hard_drive.state_keys_count,
    chronicle_memories: storage.chronicle_hard_drive.memories_count,
    chronicle_recurrences_consolidated:
      storage.chronicle_hard_drive.recurrences_consolidated,
    forge_candidates: forgeCore.listCandidateArtifacts().length,
  });
});

app.get("/api/status", (_req, res) => {
  const agentsMap: Record<string, any> = {};
  for (const [id, m] of Object.entries(AGENT_MANIFESTS)) {
    agentsMap[id] = {
      id,
      name: m.constitutional_name,
      role: m.domain,
      description: m.primary_mission,
      status: "registered",
      capabilities: m.capabilities,
    };
  }
  res.json({
    status: "gateway_healthy",
    agent_runtime_health: "not_probed",
    ecosystem: "Universe",
    constitution: {
      principles: CONSTITUTIONAL_PRINCIPLES,
      engineering_rules: ENGINEERING_RULES,
      oath: CONSTITUTIONAL_OATH,
    },
    agents: agentsMap,
    storage: buildStorageTopology(),
    timestamp: new Date().toISOString(),
  });
});

app.get("/agents", (_req, res) => {
  const list = Object.values(AGENT_MANIFESTS).map((m) => ({
    id: m.id,
    name: m.constitutional_name,
    role: m.domain,
    description: m.primary_mission,
    status: "registered",
    capabilities: m.capabilities,
    write_scopes: m.channels,
  }));
  res.json({ agents: list, count: list.length });
});

app.get("/agents/:name/status", (req, res) => {
  const name = req.params.name.toLowerCase();
  const manifest = AGENT_MANIFESTS[name];
  if (!manifest) {
    return res.status(404).json({ error: `Unknown agent: ${name}` });
  }
  res.json({
    agent: name,
    name: manifest.constitutional_name,
    role: manifest.domain,
    status: "not_probed",
    capabilities: manifest.capabilities,
    write_scopes: manifest.channels,
    timestamp: new Date().toISOString(),
  });
});

app.get("/agents/:name/data", async (req, res) => {
  const name = req.params.name.toLowerCase();
  if (!AGENT_MANIFESTS[name]) {
    return res.status(404).json({ error: `Unknown agent: ${name}` });
  }
  const broker = String(req.query.broker || "all").toLowerCase();
  const pyRich = await runNexusCoordinationBridge({
    action: "dashboard_data",
    agent_id: name,
    broker,
  });
  if (pyRich && pyRich.id) {
    return res.json(pyRich);
  }
  return res.status(503).json({
    id: name,
    status: "unavailable",
    error: `${name} dashboard evidence service did not respond; cached sample metrics are suppressed.`,
  });
});

// ── 4. Chronicle Authoritative Storage & Constitutional Endpoints ────────────
app.get("/api/chronicle/storage", (_req, res) => {
  const overview = chronicle.getChronicleOverview();
  res.json({
    status: "ok",
    constitution: {
      principles: CONSTITUTIONAL_PRINCIPLES,
      engineering_rules: ENGINEERING_RULES,
      oath: CONSTITUTIONAL_OATH,
    },
    topology: buildStorageTopology(),
    recent_memories: overview.recent_memories,
    recent_episodes: overview.recent_episodes,
    recent_lessons: chronicle.adviceFor("forge", "oracle.pilot"),
  });
});

// ── 5. Oracle V1/V2 <-> Forge V2 + Atlas Multi-Timeframe Discovery Endpoints ─
app.get("/internal/oracle/forge/catalog", (_req, res) => {
  const snapshot = oracleAdapter.getCatalogSnapshot();
  res.json(snapshot);
});

app.get("/internal/oracle/forge/incumbent", (req, res) => {
  const symbol = String(req.query.symbol || "GBPUSD").toUpperCase();
  const regime = req.query.regime ? String(req.query.regime) : undefined;
  const incumbent = oracleAdapter.getFrozenIncumbent(symbol, regime);
  const readiness = oracleAdapter.inspectSymbolReadiness(symbol);
  if (!incumbent) {
    return res.status(404).json({
      error: `No frozen V1 incumbent found for ${symbol}`,
      readiness,
    });
  }
  res.json({
    status: "ok",
    incumbent,
    readiness,
  });
});

app.get("/api/forge/pilot/status", async (req, res) => {
  const result = await runNexusCoordinationBridge({ action: "forge_action", forge_action: "status", params: req.query || {} });
  if (result && result.status !== "unavailable") return res.json(result);
  return res.status(503).json({ status: "unavailable", candidates: [], message: "Forge V2 service did not respond; no sample candidates were substituted." });
});

app.post("/api/forge/v2/discover", async (req, res) => {
  const result = await runNexusCoordinationBridge({ action: "forge_action", forge_action: "discover_v2", params: req.body || {} });
  if (result && result.status !== "unavailable") return res.json(result);
  return res.status(503).json({ status: "unavailable", message: "Forge V2 did not respond; training was not started." });
});

app.post("/api/forge/pilot/run", async (req, res) => {
  const code = String(req.body?.candidate_code || "ALL").toUpperCase();
  const styleMap: Record<string, string> = {
    V2_SCALPING: "SCALPING", SCALPING: "SCALPING", SCALP_PRECISION: "SCALP_PRECISION",
    V2_DAY_TRADING: "DAY_TRADING", DAY_TRADING: "DAY_TRADING", INTRADAY_CONTINUATION: "INTRADAY_CONTINUATION",
    V2_SWING_TRADING: "SWING_TRADING", SWING_TRADING: "SWING_TRADING", SWING_PULLBACK: "SWING_PULLBACK",
    V2_RANGE_SPECIALIST: "RANGE_SPECIALIST", RANGE_SPECIALIST: "RANGE_SPECIALIST", RANGE_MEAN_REVERSION: "RANGE_MEAN_REVERSION",
  };
  const result = await runNexusCoordinationBridge({ action: "forge_action", forge_action: "discover_v2", params: { ...req.body, strategy_family: styleMap[code] || req.body?.strategy_family || "ALL" } });
  if (result && result.status !== "unavailable") return res.json(result);
  return res.status(503).json({ status: "unavailable", message: "Forge V2 did not respond; pilot evaluation was not started." });
});

app.post("/api/forge/candidates/:id/transition", async (req, res) => {
  const result = await runNexusCoordinationBridge({ action: "forge_action", forge_action: "transition_candidate", params: { ...req.body, candidate_id: req.params.id } });
  if (result && result.status !== "unavailable") return res.json(result);
  return res.status(503).json({ status: "unavailable", message: "Governed Forge service did not respond; candidate state was not changed." });
});

app.post("/api/forge/oracle/rollback", async (req, res) => {
  const result = await runNexusCoordinationBridge({ action: "forge_action", forge_action: "rollback_candidate", params: req.body || {} });
  if (result && result.status !== "unavailable") return res.json(result);
  return res.status(503).json({ status: "unavailable", message: "Governed Forge service did not respond; rollback was not performed." });
});

app.post("/api/forge/oracle/outbox", async (req, res) => {
  const result = await runNexusCoordinationBridge({ action: "forge_action", forge_action: "flush_outbox", params: req.body || {} });
  if (result && result.status !== "unavailable") return res.json(result);
  return res.status(503).json({ status: "unavailable", message: "Forge delivery acknowledgement is unavailable; no sync is reported." });
});

app.post("/api/forge/v2/retrain-from-feedback", async (req, res) => {
  const result = await runNexusCoordinationBridge({ action: "forge_action", forge_action: "surgical_retrain", params: req.body || {} });
  if (result && result.status !== "unavailable") return res.json(result);
  return res.status(503).json({ status: "unavailable", message: "Forge V2 did not respond; retraining was not started." });
});

app.get("/api/forge/v2/shadow/status", async (req, res) => {
  const result = await runNexusCoordinationBridge({ action: "forge_action", forge_action: "status", params: req.query || {} });
  if (result && result.status !== "unavailable") return res.json({ ...result, shadow_runner_status: "Unavailable · no authoritative runner-status record is exposed" });
  return res.status(503).json({ status: "unavailable", message: "Forge V2 status service did not respond." });
});

app.post("/api/forge/v2/shadow/run", async (req, res) => {
  const result = await runNexusCoordinationBridge({ action: "forge_action", forge_action: "shadow_run", params: req.body || {} });
  if (result && result.status !== "unavailable") return res.json(result);
  return res.status(503).json({ status: "unavailable", message: "Forge V2 did not respond; shadow evaluation was not started." });
});

app.post("/api/forge/cost-profile", async (req, res) => {
  const result = await runNexusCoordinationBridge({ action: "forge_action", forge_action: "register_cost_profile", params: req.body || {} });
  if (result && result.status !== "unavailable") return res.json(result);
  return res.status(503).json({ status: "unavailable", message: "Cost profile service did not respond; no assumptions were stored." });
});

app.post("/api/forge/catalog/sync", async (_req, res) => {
  const result = await runNexusCoordinationBridge({ action: "forge_action", forge_action: "sync_catalog", params: {} });
  if (result && result.status !== "unavailable") return res.json(result);
  return res.status(503).json({ status: "unavailable", message: "Forge V2 did not respond; catalog sync was not reported." });
});

// ── 6. Agent & Ecosystem Chat (Grounded in Chronicle State + Python Atlas V2) ──
function runAtlasPythonInvestigation(
  query: string,
  mode: string = "auto"
): Promise<any> {
  return new Promise((resolve) => {
    const scriptPath = path.join(
      __dirname,
      "Atlas",
      "api",
      "frontend_chat_bridge.py"
    );
    const child = spawn("python3", [scriptPath], {
      cwd: __dirname,
      env: { ...process.env, PYTHONPATH: __dirname },
    });

    let stdout = "";
    const timer = setTimeout(() => {
      try {
        child.kill("SIGKILL");
      } catch {}
      resolve(null);
    }, 22000);

    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });

    child.on("error", () => {
      clearTimeout(timer);
      resolve(null);
    });

    child.on("close", (code) => {
      clearTimeout(timer);
      if (code === 0 && stdout.trim()) {
        try {
          resolve(JSON.parse(stdout.trim()));
          return;
        } catch {}
      }
      resolve(null);
    });

    child.stdin.write(JSON.stringify({ query, mode }));
    child.stdin.end();
  });
}

const CHAT_HISTORY_LOG_PATH = path.join(
  __dirname,
  "Chronicle",
  "memory",
  "research",
  "chat_history_log.jsonl"
);

function appendChatHistoryLog(
  agentName: string,
  userMessage: string,
  mode: string,
  reply: string,
  dossier?: any
): void {
  try {
    fs.mkdirSync(path.dirname(CHAT_HISTORY_LOG_PATH), { recursive: true });
    const record = {
      timestamp: new Date().toISOString(),
      agent: agentName,
      mode,
      question: userMessage,
      chronicle_status: dossier?.chronicle_status || "live_agent_memory",
      chronicle_memory_id: dossier?.chronicle_memory_id || null,
      reply_preview: reply.slice(0, 320),
      reply,
    };
    fs.appendFileSync(
      CHAT_HISTORY_LOG_PATH,
      JSON.stringify(record) + "\n",
      "utf-8"
    );
  } catch {}
}

app.get("/api/chronicle/chat-history", (req, res) => {
  const limit = Math.min(100, Math.max(1, Number(req.query.limit || 25)));
  const entries: any[] = [];
  try {
    if (fs.existsSync(CHAT_HISTORY_LOG_PATH)) {
      const lines = fs
        .readFileSync(CHAT_HISTORY_LOG_PATH, "utf-8")
        .split(/\r?\n/)
        .filter((l) => l.trim().startsWith("{"));
      for (const line of lines.slice(-limit)) {
        try {
          entries.push(JSON.parse(line));
        } catch {}
      }
    }
  } catch {}
  res.json({
    status: "ok",
    total_logged: entries.length,
    interactions: entries.reverse(),
  });
});

function detectCrossAgentReferral(
  currentAgent: string,
  msgLow: string
): string | null {
  if (
    currentAgent !== "oracle" &&
    /\b(open trade|win rate|winning rate|how many wins|pnl|portfolio|mt5|c3|ctrader|executed trade)\b/.test(
      msgLow
    )
  ) {
    return "**Collective Referral (via Chronicle & Nexus)**: For full execution logs, open setups, and per-symbol win rates from memory, you can also talk directly to **Oracle** on the Oracle page.";
  }
  if (
    currentAgent !== "pulse" &&
    /\b(social media|who posted|twitter|reddit|mastodon|bluesky|influencer|crowd sentiment)\b/.test(
      msgLow
    )
  ) {
    return "**Collective Referral (via Chronicle & Nexus)**: For the full live stream of social media accounts and who posted what, you can also check **Pulse** on the Pulse page.";
  }
  if (
    currentAgent !== "sentinel" &&
    /\b(world news|breaking news|headlines today|al jazeera|bbc|cnbc|geopolitics|what's happening in the world)\b/.test(
      msgLow
    )
  ) {
    return "**Collective Referral (via Chronicle & Nexus)**: For all 18+ real-time global & financial news wires today, you can also visit **Sentinel** on the Sentinel page.";
  }
  if (
    currentAgent !== "aegis" &&
    /\b(security|firewall|kill switch|veto|compliance|threat|risk law)\b/.test(
      msgLow
    )
  ) {
    return "**Collective Referral (via Chronicle & Nexus)**: For constitutional risk guardrails and security audits, you can also consult **Aegis** on the Aegis page.";
  }
  if (
    currentAgent !== "forge" &&
    /\b(last trading session|retrain|mistake clinic|shadow runner|7-timeframe|lockbox training)\b/.test(
      msgLow
    )
  ) {
    return "**Collective Referral (via Chronicle & Nexus)**: For 7-timeframe strategy retraining and the last trading session audit, you can also talk to **Forge** on the Forge page.";
  }
  if (
    currentAgent !== "atlas" &&
    /\b(formula|equation|wavelength|wikipedia|arxiv|pubmed|peer-reviewed|scientific|history of|who invented|speed of|gravity|compound interest|kinetic energy)\b/.test(
      msgLow
    )
  ) {
    return "**Collective Referral (via Chronicle & Nexus)**: For deep multi-source academic & scientific research across Wikipedia, arXiv, Crossref, and PubMed, **Atlas** is our specialist research desk.";
  }
  return null;
}

function runNexusCoordinationBridge(payload: Record<string, any>): Promise<any> {
  return new Promise((resolve) => {
    const scriptPath = path.join(
      __dirname,
      "Nexus",
      "api",
      "coordination_bridge.py"
    );
    const child = spawn(PYTHON_BIN, [scriptPath], {
      cwd: __dirname,
      env: { ...process.env, PYTHONPATH: __dirname },
    });

    let stdout = "";
    const timer = setTimeout(() => {
      try {
        child.kill("SIGKILL");
      } catch {}
      resolve(null);
    }, 18000);

    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });

    child.on("error", () => {
      clearTimeout(timer);
      resolve(null);
    });

    child.on("close", (code) => {
      clearTimeout(timer);
      if (code === 0 && stdout.trim()) {
        try {
          resolve(JSON.parse(stdout.trim()));
          return;
        } catch {}
      }
      resolve(null);
    });

    child.stdin.write(JSON.stringify(payload));
    child.stdin.end();
  });
}

// ── Hybrid Dual-LLM Layer: Cloud Gemini (gemini-4-hydrogen) + Free Local Ollama ──
const DEFAULT_GEMINI_MODEL = "gemini-4-hydrogen";
const DEFAULT_OLLAMA_HOST = (
  process.env.OLLAMA_HOST || "http://127.0.0.1:11434"
).replace(/\/+$/, "");

let ollamaDiscoveryCache: {
  checkedAt: number;
  available: boolean;
  model: string;
  models: string[];
} = {
  checkedAt: 0,
  available: false,
  model: "",
  models: [],
};

function getConfiguredGeminiApiKey(): string {
  const key = (
    process.env.GEMINI_API_KEY ||
    process.env.NEXUS_GEMINI_KEY ||
    ""
  ).trim();
  if (!key || key === "MY_GEMINI_API_KEY" || key === "YOUR_GEMINI_API_KEY") {
    return "";
  }
  return key;
}

function getGeminiClient(): GoogleGenAI | null {
  const apiKey = getConfiguredGeminiApiKey();
  if (!apiKey) return null;
  return new GoogleGenAI({
    apiKey,
    httpOptions: {
      headers: {
        "User-Agent": "aistudio-build",
      },
    },
  });
}

async function discoverLocalOllamaServer(force = false): Promise<{
  available: boolean;
  model: string;
  models: string[];
  host: string;
}> {
  const now = Date.now();
  if (!force && now - ollamaDiscoveryCache.checkedAt < 30000) {
    return { ...ollamaDiscoveryCache, host: DEFAULT_OLLAMA_HOST };
  }
  const configuredModel = (process.env.OLLAMA_MODEL || "").trim();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 700);
  try {
    const resp = await fetch(`${DEFAULT_OLLAMA_HOST}/api/tags`, {
      signal: controller.signal,
    });
    clearTimeout(timer);
    if (!resp.ok) throw new Error("Ollama tags not ok");
    const data: any = await resp.json();
    const names: string[] = Array.isArray(data?.models)
      ? data.models
          .map((m: any) => String(m?.name || "").trim())
          .filter(Boolean)
      : [];
    let chosen = configuredModel;
    if (!chosen && names.length > 0) {
      const preferred = [
        "llama3.2",
        "qwen2.5",
        "llama3.1",
        "llama3",
        "mistral",
        "gemma2",
        "phi3",
      ];
      for (const pref of preferred) {
        const hit = names.find((n) => n.toLowerCase().startsWith(pref));
        if (hit) {
          chosen = hit;
          break;
        }
      }
      if (!chosen) chosen = names[0];
    }
    ollamaDiscoveryCache = {
      checkedAt: now,
      available: Boolean(chosen),
      model: chosen,
      models: names,
    };
  } catch {
    clearTimeout(timer);
    ollamaDiscoveryCache = {
      checkedAt: now,
      available: false,
      model: configuredModel,
      models: [],
    };
  }
  return { ...ollamaDiscoveryCache, host: DEFAULT_OLLAMA_HOST };
}

async function getDualLlmStatus(force = false) {
  const geminiKey = getConfiguredGeminiApiKey();
  const ollama = await discoverLocalOllamaServer(force);
  const activeProvider = geminiKey
    ? "gemini"
    : ollama.available
    ? "ollama"
    : "deterministic_sovereign";
  return {
    active_provider: activeProvider,
    hybrid_mode: "dual_llm_plus_deterministic_grounding",
    gemini: {
      configured: Boolean(geminiKey),
      model: DEFAULT_GEMINI_MODEL,
    },
    ollama: {
      available: ollama.available,
      host: ollama.host,
      active_model: ollama.model || "llama3.2",
      installed_models: ollama.models,
    },
  };
}

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("LLM timeout")), ms);
    promise
      .then((val) => {
        clearTimeout(timer);
        resolve(val);
      })
      .catch((err) => {
        clearTimeout(timer);
        reject(err);
      });
  });
}

async function generateWithDualLlm(
  prompt: string,
  systemInstruction: string
): Promise<{ text: string | null; provider: string }> {
  const order = (process.env.LLM_PROVIDER_ORDER || "gemini,ollama").toLowerCase();
  const preferOllamaFirst = order.startsWith("ollama");

  const tryGemini = async (): Promise<string | null> => {
    const ai = getGeminiClient();
    if (!ai) return null;
    try {
      const response = await withTimeout(
        ai.models.generateContent({
          model: DEFAULT_GEMINI_MODEL,
          contents: prompt,
          config: {
            systemInstruction,
            thinkingConfig: { thinkingLevel: ThinkingLevel.LOW },
          },
        }),
        3500
      );
      const text = response.text;
      return text && text.trim() ? text.trim() : null;
    } catch {
      return null;
    }
  };

  const tryOllama = async (): Promise<string | null> => {
    const ollama = await discoverLocalOllamaServer();
    if (!ollama.available || !ollama.model) return null;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 3500);
    try {
      const resp = await fetch(`${DEFAULT_OLLAMA_HOST}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: ollama.model,
          stream: false,
          messages: [
            { role: "system", content: systemInstruction },
            { role: "user", content: prompt },
          ],
        }),
        signal: controller.signal,
      });
      clearTimeout(timer);
      if (!resp.ok) return null;
      const data: any = await resp.json();
      const content = data?.message?.content;
      return typeof content === "string" && content.trim()
        ? content.trim()
        : null;
    } catch {
      clearTimeout(timer);
      return null;
    }
  };

  if (preferOllamaFirst) {
    const ol = await tryOllama();
    if (ol) return { text: ol, provider: "ollama" };
    const gm = await tryGemini();
    if (gm) return { text: gm, provider: "gemini" };
  } else {
    const gm = await tryGemini();
    if (gm) return { text: gm, provider: "gemini" };
    const ol = await tryOllama();
    if (ol) return { text: ol, provider: "ollama" };
  }
  return { text: null, provider: "deterministic_sovereign" };
}

async function classifyIntentWithDualLlm(userMessage: string): Promise<{
  target_agent: string;
  domain: string;
  intent_topic: string;
  is_ecosystem_question: boolean;
  provider: string;
} | null> {
  const ai = getGeminiClient();
  if (ai) {
    try {
      const response = await withTimeout(
        ai.models.generateContent({
          model: DEFAULT_GEMINI_MODEL,
          contents: `Classify this user message for the 9-agent Universe ecosystem: "${userMessage}"`,
          config: {
            systemInstruction:
              "You are Nexus, the intent router for the 9-agent Universe AI ecosystem. " +
              "Map the user's message to the single best primary agent:\n" +
              "- oracle (domain: trading): today's trades, did we make money, profit/loss, P&L, account balance, win rate, open positions, EURUSD/GBPUSD/XAUUSD/USDJPY signals.\n" +
              "- forge (domain: training): last trading session review, Mistake Clinic, strategy retraining, 7-timeframe lockbox backtests.\n" +
              "- aegis (domain: governance): is our account safe, risk rules, Stop-Loss < Take-Profit law, blocked/vetoed bad trades.\n" +
              "- sentinel (domain: news): world news, financial headlines, macro events, geopolitics.\n" +
              "- pulse (domain: social): social media posts, what people/traders are saying on Mastodon/Bluesky/HackerNews, crowd sentiment.\n" +
              "- chronicle (domain: memory): what have we learned or saved in shared memory.\n" +
              "- genesis (domain: creation): evolving new strategy genomes or Monte Carlo blueprints.\n" +
              "- atlas (domain: research): general knowledge, science, history, formulas, equations, definitions, or how good Atlas's 5 research desks are.",
            thinkingConfig: { thinkingLevel: ThinkingLevel.LOW },
            responseMimeType: "application/json",
            responseSchema: {
              type: Type.OBJECT,
              properties: {
                target_agent: { type: Type.STRING },
                domain: { type: Type.STRING },
                intent_topic: { type: Type.STRING },
                is_ecosystem_question: { type: Type.BOOLEAN },
              },
            },
          },
        }),
        3500
      );
      const raw = (response.text ?? "").trim();
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && parsed.target_agent) {
          return {
            target_agent: String(parsed.target_agent).toLowerCase(),
            domain: String(parsed.domain || "research").toLowerCase(),
            intent_topic: String(parsed.intent_topic || "general"),
            is_ecosystem_question: Boolean(parsed.is_ecosystem_question),
            provider: "gemini",
          };
        }
      }
    } catch {
      // Fall through to Ollama if Gemini times out or fails
    }
  }

  const ollama = await discoverLocalOllamaServer();
  if (!ollama.available) return null;
  const { text, provider } = await generateWithDualLlm(
    `User question: "${userMessage}"\nReturn JSON with keys: target_agent ("oracle"|"forge"|"aegis"|"sentinel"|"pulse"|"chronicle"|"genesis"|"atlas"), domain ("trading"|"training"|"governance"|"news"|"social"|"memory"|"creation"|"research"), intent_topic, is_ecosystem_question (boolean).`,
    "Return ONLY valid JSON."
  );
  if (text) {
    try {
      const cleaned = text.replace(/^```(?:json)?\s*|\s*```$/gi, "").trim();
      const parsed = JSON.parse(cleaned);
      if (parsed && parsed.target_agent) {
        return {
          target_agent: String(parsed.target_agent).toLowerCase(),
          domain: String(parsed.domain || "research").toLowerCase(),
          intent_topic: String(parsed.intent_topic || "general"),
          is_ecosystem_question: Boolean(parsed.is_ecosystem_question),
          provider,
        };
      }
    } catch {}
  }
  return null;
}

async function handleAgentChat(
  agentName: string,
  userMessage: string,
  mode: string = "auto"
): Promise<{
  reply: string;
  dossier?: any;
  trace?: any;
  referrals?: any[];
  llm_layer?: any;
}> {
  const id = agentName.toLowerCase();
  const llmStatus = await getDualLlmStatus();

  // When talking to Nexus, execute the full Python multi-agent DAG coordination session
  // (which now also runs the Dual-LLM semantic intent classifier in Python)
  const stripAsterisks = (s: string) =>
    String(s || "")
      .replace(/\*\*([^*]+)\*\*/g, "$1")
      .replace(/\*\*/g, "");

  if (id === "nexus") {
    const trace = await runNexusCoordinationBridge({
      action: "coordinate",
      query: userMessage,
      target_agent: "nexus",
    });
    if (trace && trace.human_synthesis) {
      const cleanSyn = stripAsterisks(trace.human_synthesis);
      appendChatHistoryLog(
        id,
        userMessage,
        mode,
        cleanSyn,
        trace
      );
      return {
        reply: cleanSyn,
        dossier: trace,
        trace,
        referrals: trace.referrals || [],
        llm_layer: trace.llm_layer || llmStatus,
      };
    }
  }

  // Oracle account answers must come from the broker-snapshot/DAG path. The
  // legacy TypeScript reply builder contains historical sample values and is
  // not an authoritative account-data source.
  if (id === "oracle") {
    const trace = await runNexusCoordinationBridge({
      action: "coordinate",
      query: userMessage,
      target_agent: "oracle",
    });
    if (trace && trace.human_synthesis) {
      const cleanReply = stripAsterisks(trace.human_synthesis);
      appendChatHistoryLog(id, userMessage, mode, cleanReply, trace);
      return {
        reply: cleanReply,
        dossier: trace,
        trace,
        referrals: trace.referrals || [],
        llm_layer: trace.llm_layer || llmStatus,
      };
    }
    const unavailable = "Oracle’s live account summary is unavailable because the broker evidence service did not respond. No account figures are inferred from old training files.";
    appendChatHistoryLog(id, userMessage, mode, unavailable, { status: "unavailable" });
    return { reply: unavailable, dossier: { status: "unavailable" }, llm_layer: llmStatus };
  }

  if (id === "atlas") {
    const pyDossier = await runAtlasPythonInvestigation(userMessage, mode);
    if (pyDossier && pyDossier.readable_reply) {
      const referral = detectCrossAgentReferral(
        "atlas",
        userMessage.toLowerCase()
      );
      const refRes = await runNexusCoordinationBridge({
        action: "referrals",
        query: userMessage,
        primary_agent: "atlas",
        participating_agents: ["atlas", "chronicle"],
      });
      const finalReply = stripAsterisks(
        pyDossier.readable_reply + (referral ? `\n\n---\n${referral}` : "")
      );
      appendChatHistoryLog(id, userMessage, mode, finalReply, pyDossier);
      return {
        reply: finalReply,
        dossier: pyDossier,
        referrals: refRes?.referrals || [],
        llm_layer: llmStatus,
      };
    }
  }

  // Agent chat must be grounded in the same Python coordination/data path as
  // the dashboards. The legacy TypeScript fallback embeds example metrics and
  // canned trading claims, so it is never presented as a live agent answer.
  const trace = await runNexusCoordinationBridge({
    action: "coordinate",
    query: userMessage,
    target_agent: id,
  });
  if (trace && trace.human_synthesis) {
    const groundedReply = stripAsterisks(trace.human_synthesis);
    appendChatHistoryLog(id, userMessage, mode, groundedReply, trace);
    return {
      reply: groundedReply,
      dossier: trace,
      trace,
      referrals: trace.referrals || [],
      llm_layer: trace.llm_layer || llmStatus,
    };
  }
  const unavailable = `${AGENT_MANIFESTS[id]?.constitutional_name || id} could not return a verified response because its evidence service did not respond. No sample answer was substituted.`;
  appendChatHistoryLog(id, userMessage, mode, unavailable, { status: "unavailable" });
  return { reply: unavailable, dossier: { status: "unavailable" }, llm_layer: llmStatus };

  return { reply: unavailable, dossier: { status: "unavailable" }, llm_layer: llmStatus };
}

// ── Oracle UI routes use the Python broker-snapshot service as source of truth ──
const PYTHON_API_ORIGIN = process.env.UNIVERSE_PYTHON_API_ORIGIN || "http://127.0.0.1:8000";

function isLocalBrowserRequest(req: any): boolean {
  const peer = String(req.socket?.remoteAddress || req.ip || "").toLowerCase();
  const localPeer = ["127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"].includes(peer);
  if (!localPeer) return false;
  const origin = String(req.get?.("origin") || "");
  if (!origin) return true;
  try {
    const host = new URL(origin).hostname.toLowerCase().replace(/^\[|\]$/g, "");
    return ["127.0.0.1", "::1", "localhost"].includes(host);
  } catch {
    return false;
  }
}

async function proxyPythonOracleApi(req: any, res: any, route: string): Promise<void> {
  try {
    const target = new URL(route, PYTHON_API_ORIGIN);
    if (!["127.0.0.1", "localhost", "::1"].includes(target.hostname.replace(/^\[|\]$/g, "").toLowerCase())) {
      res.status(503).json({ status: "unavailable", message: "Oracle API proxy must target this computer." });
      return;
    }
    const method = String(req.method || "GET").toUpperCase();
    const response = await fetch(target, {
      method,
      headers: method === "GET" ? undefined : { "content-type": "application/json" },
      body: method === "GET" ? undefined : JSON.stringify(req.body || {}),
      signal: AbortSignal.timeout(10000),
    });
    const raw = await response.text();
    res.status(response.status);
    res.type(response.headers.get("content-type") || "application/json").send(raw);
  } catch (error) {
    res.status(503).json({
      status: "unavailable",
      message: `Python Oracle API is unavailable: ${error instanceof Error ? error.name : "request failed"}`,
    });
  }
}

app.get("/api/oracle/dashboard", async (req, res) => {
  const broker = String(req.query.broker || "all").toLowerCase();
  if (!["all", "mt5", "ctrader"].includes(broker)) return res.status(400).json({ status: "error", message: "Invalid broker." });
  await proxyPythonOracleApi(req, res, `/api/oracle/dashboard?broker=${encodeURIComponent(broker)}`);
});

app.get("/api/oracle/chart", async (req, res) => {
  const symbol = String(req.query.symbol || "EURUSD").toUpperCase();
  const timeframe = String(req.query.timeframe || "M15").toUpperCase();
  const broker = String(req.query.broker || "all").toLowerCase();
  if (!/^[A-Z0-9._-]{1,32}$/.test(symbol)) return res.status(400).json({ status: "error", message: "Invalid symbol." });
  if (!["W1", "D1", "H4", "H1", "M45", "M30", "M15", "M5"].includes(timeframe)) return res.status(400).json({ status: "error", message: "Unsupported timeframe." });
  if (!["all", "mt5", "ctrader"].includes(broker)) return res.status(400).json({ status: "error", message: "Invalid broker." });
  await proxyPythonOracleApi(req, res, `/api/oracle/chart?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&broker=${encodeURIComponent(broker)}`);
});

app.get("/api/oracle/telegram/signals", async (req, res) => {
  const limit = Math.min(500, Math.max(1, Number(req.query.limit) || 100));
  await proxyPythonOracleApi(req, res, `/api/oracle/telegram/signals?limit=${limit}`);
});

app.get("/api/oracle/run/status", async (req, res) => {
  if (!isLocalBrowserRequest(req)) return res.status(403).json({ status: "forbidden", message: "Run controls are local-machine only." });
  const broker = String(req.query.broker || "").toLowerCase();
  if (broker && !["mt5", "ctrader"].includes(broker)) return res.status(400).json({ status: "error", message: "Invalid broker." });
  await proxyPythonOracleApi(req, res, `/api/oracle/run/status${broker ? `?broker=${broker}` : ""}`);
});

app.post("/api/oracle/run/start", async (req, res) => {
  if (!isLocalBrowserRequest(req)) return res.status(403).json({ status: "forbidden", message: "Run controls are local-machine only." });
  if (!req.body || !["mt5", "ctrader"].includes(String(req.body.broker || "").toLowerCase())) return res.status(400).json({ status: "error", message: "Choose MT5 or cTrader." });
  await proxyPythonOracleApi(req, res, "/api/oracle/run/start");
});

app.post("/api/oracle/run/stop", async (req, res) => {
  if (!isLocalBrowserRequest(req)) return res.status(403).json({ status: "forbidden", message: "Run controls are local-machine only." });
  if (!req.body || !["mt5", "ctrader"].includes(String(req.body.broker || "").toLowerCase())) return res.status(400).json({ status: "error", message: "Choose MT5 or cTrader." });
  await proxyPythonOracleApi(req, res, "/api/oracle/run/stop");
});

app.post("/api/oracle/execute-action", async (req, res) => {
  const data = await runNexusCoordinationBridge({
    action: "oracle_action",
    params: req.body || {},
  });
  if (!data || data.status === "unavailable") return res.status(503).json(data || { status: "unavailable", message: "Oracle action service did not respond." });
  res.json(data);
});

// ── Interactive Sentinel & Pulse Live Feed Refresh Route ──
app.post("/api/feeds/refresh", async (req, res) => {
  const agent = String(req.body?.agent || "all").toLowerCase();
  if (!["all", "sentinel", "pulse"].includes(agent)) return res.status(400).json({ status: "blocked", message: "Choose Sentinel, Pulse, or both." });
  try {
    const target = new URL("/api/feeds/refresh", PYTHON_API_ORIGIN);
    if (!["127.0.0.1", "localhost", "::1"].includes(target.hostname.replace(/^\[|\]$/g, "").toLowerCase())) {
      return res.status(503).json({ status: "unavailable", message: "Feed refresh is restricted to the local Python API." });
    }
    const response = await fetch(target, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ agent }),
      signal: AbortSignal.timeout(90000),
    });
    const raw = await response.text();
    res.status(response.status).type(response.headers.get("content-type") || "application/json").send(raw);
  } catch (error) {
    res.status(503).json({ status: "unavailable", message: `Python feed collectors did not respond: ${error instanceof Error ? error.name : "request failed"}` });
  }
});

app.get("/api/sentinel/news", async (_req, res) => {
  const pyRich = await runNexusCoordinationBridge({
    action: "dashboard_data",
    agent_id: "sentinel",
    broker: "all",
  });
  if (!pyRich) return res.status(503).json({ status: "unavailable", news: [], message: "Sentinel evidence service did not respond." });
  res.json({ status: pyRich.status || "unavailable", news: pyRich.news_articles || pyRich.world_news_feed || [] });
});

app.get("/api/pulse/social", async (_req, res) => {
  const pyRich = await runNexusCoordinationBridge({
    action: "dashboard_data",
    agent_id: "pulse",
    broker: "all",
  });
  if (!pyRich) return res.status(503).json({ status: "unavailable", posts: [], message: "Pulse evidence service did not respond." });
  res.json({ status: pyRich.status || "unavailable", posts: pyRich.social_posts || pyRich.social_posts_feed || [] });
});

// ── Interactive Chronicle, Aegis & Genesis Specialist Action Route ──
app.post("/api/specialist/action", async (req, res) => {
  const data = await runNexusCoordinationBridge({
    action: "specialist_action",
    params: req.body || {},
  });
  if (!data || data.status === "unavailable") return res.status(503).json(data || { status: "unavailable", message: "Specialist action service did not respond." });
  res.json(data);
});

// ── Genesis Durable Control-Plane Routes (/api/genesis/*) ──
app.get("/api/genesis/cases", async (_req, res) => {
  const data = await runNexusCoordinationBridge({
    action: "dashboard_data",
    agent_id: "genesis",
    broker: "all",
  });
  if (!data || data.status === "unavailable") return res.status(503).json(data || { status: "unavailable", message: "Genesis control-plane service did not respond." });
  res.json(data);
});

app.post("/api/genesis/cases/open", async (req, res) => {
  const data = await runNexusCoordinationBridge({
    action: "specialist_action",
    params: {
      agent: "genesis",
      action: "open_case",
      payload: req.body || {},
    },
  });
  if (!data || data.status === "unavailable") return res.status(503).json(data || { status: "unavailable", message: "Genesis control-plane service did not respond." });
  res.json(data);
});

app.post("/api/genesis/cases/:case_id/advance", async (req, res) => {
  const data = await runNexusCoordinationBridge({
    action: "specialist_action",
    params: {
      agent: "genesis",
      action: "advance_case",
      payload: { ...(req.body || {}), case_id: req.params.case_id },
    },
  });
  if (!data || data.status === "unavailable") return res.status(503).json(data || { status: "unavailable", message: "Genesis control-plane service did not respond." });
  res.json(data);
});

app.post("/api/genesis/cases/:case_id/approve", async (req, res) => {
  const data = await runNexusCoordinationBridge({
    action: "specialist_action",
    params: {
      agent: "genesis",
      action: "approve_release",
      payload: { ...(req.body || {}), case_id: req.params.case_id },
    },
  });
  if (!data || data.status === "unavailable") return res.status(503).json(data || { status: "unavailable", message: "Genesis control-plane service did not respond." });
  res.json(data);
});

app.post("/api/genesis/cases/:case_id/rollback", async (req, res) => {
  const data = await runNexusCoordinationBridge({
    action: "specialist_action",
    params: {
      agent: "genesis",
      action: "rollback_case",
      payload: { ...(req.body || {}), case_id: req.params.case_id },
    },
  });
  if (!data || data.status === "unavailable") return res.status(503).json(data || { status: "unavailable", message: "Genesis control-plane service did not respond." });
  res.json(data);
});

// ── Interactive Plain-English Forge Training Routes (Missing V2 Endpoints) ──
app.get("/api/forge/pilot/status", async (req, res) => {
  const data = await runNexusCoordinationBridge({
    action: "forge_action",
    forge_action: "status",
    params: req.query || {},
  });
  if (!data || data.status === "unavailable") return res.status(503).json(data || { status: "unavailable", candidates: [], message: "Forge V2 service did not respond." });
  res.json(data);
});

app.post("/api/forge/v2/retrain", async (req, res) => {
  const data = await runNexusCoordinationBridge({
    action: "forge_action",
    forge_action: "surgical_retrain",
    params: req.body || {},
  });
  if (!data || data.status === "unavailable") return res.status(503).json(data || { status: "unavailable", message: "Forge retraining service did not respond." });
  res.json(data);
});

app.post("/api/forge/candidate/transition", async (req, res) => {
  const data = await runNexusCoordinationBridge({
    action: "forge_action",
    forge_action: "transition_candidate",
    params: req.body || {},
  });
  if (!data || data.status === "unavailable") return res.status(503).json(data || { status: "unavailable", message: "Forge transition service did not respond." });
  res.json(data);
});

app.post("/api/forge/candidate/rollback", async (req, res) => {
  const data = await runNexusCoordinationBridge({
    action: "forge_action",
    forge_action: "rollback_candidate",
    params: req.body || {},
  });
  if (!data || data.status === "unavailable") return res.status(503).json(data || { status: "unavailable", message: "Forge rollback service did not respond." });
  res.json(data);
});

app.post("/api/forge/v1-catalog/sync", async (_req, res) => {
  const data = await runNexusCoordinationBridge({
    action: "forge_action",
    forge_action: "sync_catalog",
    params: {},
  });
  if (!data || data.status === "unavailable") return res.status(503).json(data || { status: "unavailable", message: "Forge catalog service did not respond." });
  res.json(data);
});

app.post("/api/forge/outbox/flush", async (_req, res) => {
  const data = await runNexusCoordinationBridge({
    action: "forge_action",
    forge_action: "flush_outbox",
    params: {},
  });
  if (!data || data.status === "unavailable") return res.status(503).json(data || { status: "unavailable", message: "Forge outbox service did not respond." });
  res.json(data);
});

app.get("/api/forge/candidate/:id", async (req, res) => {
  const data = await runNexusCoordinationBridge({
    action: "forge_action",
    forge_action: "get_candidate",
    params: { candidate_id: req.params.id },
  });
  if (!data || data.status === "unavailable") return res.status(503).json(data || { status: "unavailable", message: "Forge candidate service did not respond." });
  res.json(data);
});

app.get("/api/llm/status", async (req, res) => {
  const force = req.query.refresh === "true";
  const status = await getDualLlmStatus(force);
  res.json({
    status: "ok",
    ...status,
    timestamp: new Date().toISOString(),
  });
});

app.post("/api/llm/understand", async (req, res) => {
  const message = String(
    req.body?.message || req.body?.query || "How much did we make today?"
  ).trim();
  const intent = await classifyIntentWithDualLlm(message);
  const trace = await runNexusCoordinationBridge({
    action: "coordinate",
    query: message,
    target_agent: intent?.target_agent || "nexus",
  });
  if (!trace || !trace.human_synthesis) {
    return res.status(503).json({
      status: "unavailable",
      query: message,
      llm_intent: intent || null,
      message: "The language model classified the request, but the evidence and agent response service did not return a result.",
      llm_layer: await getDualLlmStatus(),
    });
  }
  res.json({
    status: "complete",
    query: message,
    llm_intent: intent,
    routed_primary_agent: trace?.primary_agent || intent?.target_agent || "oracle",
    reply: trace?.human_synthesis || "",
    llm_layer: await getDualLlmStatus(),
  });
});

// ── Live Institutional Nexus Coordination API (/api/nexus/*) ──
app.get("/api/nexus/status", async (_req, res) => {
  const status = await runNexusCoordinationBridge({ action: "status" });
  if (status) return res.json(status);
  return res.status(503).json({ status: "unavailable", message: "Nexus runtime health could not be queried." });
});

app.get("/api/nexus/traces", async (req, res) => {
  const limit = Math.min(50, Math.max(1, Number(req.query.limit || 12)));
  const data = await runNexusCoordinationBridge({ action: "traces", limit });
  if (!data) return res.status(503).json({ status: "unavailable", traces: [], message: "Nexus trace service did not respond." });
  res.json(data);
});

app.get("/api/nexus/memory-matrix", async (req, res) => {
  const limit = Math.min(50, Math.max(1, Number(req.query.limit || 12)));
  const data = await runNexusCoordinationBridge({
    action: "memory_matrix",
    limit,
  });
  if (!data) return res.status(503).json({ status: "unavailable", message: "Chronicle collective-memory service did not respond." });
  res.json(data);
});

app.post("/api/nexus/coordinate", async (req, res) => {
  const query =
    req.body?.query ||
    req.body?.message ||
    "Coordinate ecosystem status across all specialist desks";
  const userId = String(req.body?.user_id || "operator");
  const targetAgent = String(req.body?.target_agent || "nexus");
  const trace = await runNexusCoordinationBridge({
    action: "coordinate",
    query,
    user_id: userId,
    target_agent: targetAgent,
  });
  if (trace && trace.human_synthesis) {
    appendChatHistoryLog("nexus", query, "dag_coordinate", trace.human_synthesis, trace);
    return res.json({
      status: "complete",
      agent: "nexus",
      reply: trace.human_synthesis,
      response: trace.human_synthesis,
      trace,
      referrals: trace.referrals || [],
      timestamp: new Date().toISOString(),
    });
  }
  return res.status(503).json({ status: "unavailable", agent: "nexus", message: "Nexus could not coordinate because its evidence service did not respond; no canned synthesis was substituted." });
});

app.post("/agents/:name/chat", async (req, res) => {
  const name = req.params.name.toLowerCase();
  const message =
    req.body?.message || req.body?.query || req.body?.prompt || "Status report";
  const mode = String(req.body?.mode || "auto");
  const { reply, dossier, trace, referrals } = await handleAgentChat(
    name,
    message,
    mode
  );
  res.json({
    agent: name,
    reply,
    response: reply,
    message: reply,
    dossier: dossier || null,
    trace: trace || null,
    referrals: referrals || [],
    timestamp: new Date().toISOString(),
  });
});

app.post("/api/atlas/investigate", async (req, res) => {
  const message =
    req.body?.query ||
    req.body?.message ||
    "How good is your research right now?";
  const mode = String(req.body?.mode || "auto");
  const { reply, dossier, referrals } = await handleAgentChat(
    "atlas",
    message,
    mode
  );
  if (dossier?.status === "unavailable" || !reply) {
    return res.status(503).json({ status: "unavailable", agent: "atlas", message: reply || "Atlas research evidence is unavailable.", dossier: dossier || null });
  }
  res.json({
    status: dossier?.status || "complete",
    agent: "atlas",
    reply,
    response: reply,
    message: reply,
    dossier: dossier || null,
    referrals: referrals || [],
    timestamp: new Date().toISOString(),
  });
});

app.post("/agents/:name/query", async (req, res) => {
  const name = req.params.name.toLowerCase();
  const message =
    req.body?.query || req.body?.message || req.body?.prompt || "Status report";
  const mode = String(req.body?.mode || "auto");
  const { reply, dossier, trace, referrals } = await handleAgentChat(
    name,
    message,
    mode
  );
  res.json({
    agent: name,
    response: reply,
    result: reply,
    dossier: dossier || null,
    trace: trace || null,
    referrals: referrals || [],
    data: trace || dossier || { status: "unavailable" },
    timestamp: new Date().toISOString(),
  });
});

app.post("/ecosystem/chat", async (req, res) => {
  const message = req.body?.message || req.body?.query || "Ecosystem overview";
  const targetAgent = (req.body?.agent || "nexus").toLowerCase();
  const mode = String(req.body?.mode || "auto");
  const { reply, dossier, trace, referrals } = await handleAgentChat(
    targetAgent,
    message,
    mode
  );
  res.json({
    agent: targetAgent,
    response: reply,
    message: reply,
    dossier: dossier || null,
    trace: trace || null,
    referrals: referrals || [],
    timestamp: new Date().toISOString(),
  });
});

app.get("/logs", (_req, res) => {
  const memories = chronicle.retrieveMemories({ limit: 25 });
  res.json({
    logs: memories.map((m: ChronicleMemoryEntry) => ({
      timestamp: safeIsoTimestamp(m.last_occurred_unix || m.created_at_unix),
      agent: m.source_repository,
      level: "INFO",
      message: `[${m.pillar}/${m.domain} | x${m.occurrence_count || 1}] ${m.content}`,
    })),
  });
});

// ── 6B. Loopback-Only Server-Side GitHub Sync (uses process.env.GITHUB_TOKEN) ─
function isLoopbackOnlyRequest(req: express.Request): boolean {
  if (req.headers["x-forwarded-for"] || req.headers["forwarded"]) {
    return false;
  }
  const addr = String(req.socket?.remoteAddress || "");
  return addr === "127.0.0.1" || addr === "::1" || addr === "::ffff:127.0.0.1";
}

app.get("/api/internal/github-sync/info", async (req, res) => {
  if (!isLoopbackOnlyRequest(req)) {
    return res.status(403).json({ error: "Loopback only" });
  }
  const token = process.env.GITHUB_TOKEN || process.env.GITHUB_PAT;
  if (!token) {
    return res.status(400).json({ error: "GITHUB_TOKEN not configured" });
  }
  const owner = String(req.query.owner || "onyiameave-lang");
  const repo = String(req.query.repo || "Universe");
  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "User-Agent": "Universe-Sync-Agent",
  };
  try {
    const repoResp = await fetch(
      `https://api.github.com/repos/${owner}/${repo}`,
      { headers }
    );
    const repoData: any = await repoResp.json();
    if (!repoResp.ok) {
      return res.status(repoResp.status).json({
        error: repoData?.message || "Failed to fetch repo metadata",
      });
    }
    const defaultBranch = repoData.default_branch || "main";
    const branchesResp = await fetch(
      `https://api.github.com/repos/${owner}/${repo}/branches?per_page=20`,
      { headers }
    );
    const branchesData: any = await branchesResp.json();
    const targetBranch = String(req.query.branch || defaultBranch);
    const refResp = await fetch(
      `https://api.github.com/repos/${owner}/${repo}/git/ref/heads/${encodeURIComponent(targetBranch)}`,
      { headers }
    );
    const refData: any = await refResp.json();
    let topLevel: string[] = [];
    let latestCommitMsg = "";
    if (refResp.ok && refData?.object?.sha) {
      const commitResp = await fetch(
        `https://api.github.com/repos/${owner}/${repo}/git/commits/${refData.object.sha}`,
        { headers }
      );
      const commitData: any = await commitResp.json();
      latestCommitMsg = commitData?.message || "";
      if (commitData?.tree?.sha) {
        const treeResp = await fetch(
          `https://api.github.com/repos/${owner}/${repo}/git/trees/${commitData.tree.sha}`,
          { headers }
        );
        const treeData: any = await treeResp.json();
        topLevel = (treeData?.tree || []).map(
          (t: any) => `${t.type}:${t.path}`
        );
      }
    }
    return res.json({
      full_name: repoData.full_name,
      default_branch: defaultBranch,
      inspected_branch: targetBranch,
      head_sha: refData?.object?.sha || null,
      latest_commit_message: latestCommitMsg,
      branches: Array.isArray(branchesData)
        ? branchesData.map((b: any) => ({
            name: b.name,
            sha: b.commit?.sha,
          }))
        : [],
      top_level: topLevel,
    });
  } catch (err: any) {
    return res.status(500).json({ error: String(err?.message || err) });
  }
});

app.post("/api/internal/github-sync/push", async (req, res) => {
  if (!isLoopbackOnlyRequest(req)) {
    return res.status(403).json({ error: "Loopback only" });
  }
  const token = process.env.GITHUB_TOKEN || process.env.GITHUB_PAT;
  if (!token) {
    return res.status(400).json({ error: "GITHUB_TOKEN not configured" });
  }
  const owner = String(req.body?.owner || "onyiameave-lang");
  const repo = String(req.body?.repo || "Universe");
  const branch = String(req.body?.branch || "main");
  const prefix = String(req.body?.prefix || "").replace(/^\/+|\/+$/g, "");
  const commitMessage = String(
    req.body?.commit_message ||
      "Upgrade Nexus institutional coordination, strict specialist routing, and plain-English responses"
  );
  const files: string[] = Array.isArray(req.body?.files) ? req.body.files : [];
  if (!files.length) {
    return res.status(400).json({ error: "No files specified" });
  }

  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "Content-Type": "application/json",
    "User-Agent": "Universe-Sync-Agent",
  };

  try {
    const refResp = await fetch(
      `https://api.github.com/repos/${owner}/${repo}/git/ref/heads/${encodeURIComponent(branch)}`,
      { headers }
    );
    const refData: any = await refResp.json();
    if (!refResp.ok || !refData?.object?.sha) {
      return res.status(refResp.status).json({
        error: refData?.message || `Branch ${branch} not found`,
      });
    }
    const baseCommitSha = refData.object.sha;
    const baseCommitResp = await fetch(
      `https://api.github.com/repos/${owner}/${repo}/git/commits/${baseCommitSha}`,
      { headers }
    );
    const baseCommitData: any = await baseCommitResp.json();
    const baseTreeSha = baseCommitData?.tree?.sha;
    if (!baseTreeSha) {
      return res.status(500).json({ error: "Could not resolve base tree SHA" });
    }

    const treeItems: Array<{
      path: string;
      mode: string;
      type: string;
      sha: string;
    }> = [];
    const pushedPaths: string[] = [];

    for (const relRaw of files) {
      const cleanRel = String(relRaw).replace(/^\.\/+/, "").replace(/^\/+/, "");
      const absPath = path.resolve(__dirname, cleanRel);
      if (!absPath.startsWith(path.resolve(__dirname) + path.sep)) {
        continue;
      }
      if (!fs.existsSync(absPath) || !fs.statSync(absPath).isFile()) {
        continue;
      }
      const contentB64 = fs.readFileSync(absPath).toString("base64");
      const blobResp = await fetch(
        `https://api.github.com/repos/${owner}/${repo}/git/blobs`,
        {
          method: "POST",
          headers,
          body: JSON.stringify({ content: contentB64, encoding: "base64" }),
        }
      );
      const blobData: any = await blobResp.json();
      if (!blobResp.ok || !blobData?.sha) {
        return res.status(blobResp.status).json({
          error: `Failed creating blob for ${cleanRel}: ${blobData?.message || "unknown error"}`,
        });
      }
      const remotePath = prefix ? `${prefix}/${cleanRel}` : cleanRel;
      treeItems.push({
        path: remotePath,
        mode: "100644",
        type: "blob",
        sha: blobData.sha,
      });
      pushedPaths.push(remotePath);
    }

    if (!treeItems.length) {
      return res.status(400).json({ error: "No valid local files to push" });
    }

    const newTreeResp = await fetch(
      `https://api.github.com/repos/${owner}/${repo}/git/trees`,
      {
        method: "POST",
        headers,
        body: JSON.stringify({ base_tree: baseTreeSha, tree: treeItems }),
      }
    );
    const newTreeData: any = await newTreeResp.json();
    if (!newTreeResp.ok || !newTreeData?.sha) {
      return res.status(newTreeResp.status).json({
        error: `Failed creating tree: ${newTreeData?.message || "unknown error"}`,
      });
    }

    const newCommitResp = await fetch(
      `https://api.github.com/repos/${owner}/${repo}/git/commits`,
      {
        method: "POST",
        headers,
        body: JSON.stringify({
          message: commitMessage,
          tree: newTreeData.sha,
          parents: [baseCommitSha],
        }),
      }
    );
    const newCommitData: any = await newCommitResp.json();
    if (!newCommitResp.ok || !newCommitData?.sha) {
      return res.status(newCommitResp.status).json({
        error: `Failed creating commit: ${newCommitData?.message || "unknown error"}`,
      });
    }

    const updateRefResp = await fetch(
      `https://api.github.com/repos/${owner}/${repo}/git/refs/heads/${encodeURIComponent(branch)}`,
      {
        method: "PATCH",
        headers,
        body: JSON.stringify({ sha: newCommitData.sha, force: false }),
      }
    );
    const updateRefData: any = await updateRefResp.json();
    if (!updateRefResp.ok) {
      return res.status(updateRefResp.status).json({
        error: `Failed updating branch ${branch}: ${updateRefData?.message || "unknown error"}`,
      });
    }

    return res.json({
      status: "pushed",
      repo: `${owner}/${repo}`,
      branch,
      commit_sha: newCommitData.sha,
      commit_url: `https://github.com/${owner}/${repo}/commit/${newCommitData.sha}`,
      files_pushed_count: pushedPaths.length,
      files_pushed: pushedPaths,
    });
  } catch (err: any) {
    return res.status(500).json({ error: String(err?.message || err) });
  }
});

app.post("/api/internal/github-sync/pull", async (req, res) => {
  if (!isLoopbackOnlyRequest(req)) {
    return res.status(403).json({ error: "Loopback only" });
  }
  const token = process.env.GITHUB_TOKEN || process.env.GITHUB_PAT;
  if (!token) {
    return res.status(400).json({ error: "GITHUB_TOKEN not configured" });
  }
  const owner = String(req.body?.owner || "onyiameave-lang");
  const repo = String(req.body?.repo || "Universe");
  const branch = String(req.body?.branch || "oracle-v2");
  const baseSha = String(req.body?.base_sha || "");
  const dryRun = Boolean(req.body?.dry_run);

  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "User-Agent": "Universe-Sync-Agent",
  };

  try {
    if (baseSha) {
      const compResp = await fetch(
        `https://api.github.com/repos/${owner}/${repo}/compare/${encodeURIComponent(baseSha)}...${encodeURIComponent(branch)}`,
        { headers }
      );
      const compData: any = await compResp.json();
      if (!compResp.ok) {
        return res.status(compResp.status).json({
          error: compData?.message || "Failed comparing commits",
        });
      }
      const commits = (compData.commits || []).map((c: any) => ({
        sha: c.sha,
        message: c.commit?.message || "",
        date: c.commit?.author?.date || "",
        author: c.commit?.author?.name || "",
      }));
      const changedFiles = compData.files || [];
      const pulledFiles: Array<{ filename: string; status: string; changes: number }> = [];

      for (const f of changedFiles) {
        const relPath = String(f.filename || "").replace(/^\/+/, "");
        if (!relPath) continue;
        const absPath = path.resolve(__dirname, relPath);
        if (!absPath.startsWith(path.resolve(__dirname) + path.sep)) continue;

        if (!dryRun) {
          if (f.status === "removed") {
            if (fs.existsSync(absPath)) {
              fs.unlinkSync(absPath);
            }
          } else if (f.sha) {
            const blobResp = await fetch(
              `https://api.github.com/repos/${owner}/${repo}/git/blobs/${f.sha}`,
              { headers }
            );
            const blobData: any = await blobResp.json();
            if (blobResp.ok && blobData?.content) {
              const buf = Buffer.from(
                String(blobData.content).replace(/\s+/g, ""),
                blobData.encoding === "base64" ? "base64" : "utf-8"
              );
              fs.mkdirSync(path.dirname(absPath), { recursive: true });
              fs.writeFileSync(absPath, buf);
            }
          }
        }
        pulledFiles.push({
          filename: relPath,
          status: f.status,
          changes: f.changes || 0,
        });
      }

      return res.json({
        status: dryRun ? "dry_run" : "pulled",
        repo: `${owner}/${repo}`,
        branch,
        base_sha: baseSha,
        head_sha: compData?.commits?.length
          ? compData.commits[compData.commits.length - 1].sha
          : baseSha,
        ahead_by: compData.ahead_by,
        behind_by: compData.behind_by,
        commits,
        files_count: pulledFiles.length,
        files: pulledFiles,
      });
    }

    // Fallback: list latest 15 commits on branch
    const commitsResp = await fetch(
      `https://api.github.com/repos/${owner}/${repo}/commits?sha=${encodeURIComponent(branch)}&per_page=15`,
      { headers }
    );
    const commitsData: any = await commitsResp.json();
    return res.json({
      status: "commits_listed",
      branch,
      commits: Array.isArray(commitsData)
        ? commitsData.map((c: any) => ({
            sha: c.sha,
            message: c.commit?.message || "",
            date: c.commit?.author?.date || "",
          }))
        : [],
    });
  } catch (err: any) {
    return res.status(500).json({ error: String(err?.message || err) });
  }
});

// ── 7. Serve Original Universe Static Frontend (/frontend) ───────────────────
const frontendDir = path.join(__dirname, "frontend");
app.use(express.static(frontendDir));

app.get("/", (_req, res) => {
  res.sendFile(path.join(frontendDir, "index.html"));
});

const PORT = Number(process.env.PORT) || 3000;
app.listen(PORT, "0.0.0.0", () => {
  console.log(
    `[Universe Gateway + Forge V2] Listening on http://0.0.0.0:${PORT}`
  );
});
