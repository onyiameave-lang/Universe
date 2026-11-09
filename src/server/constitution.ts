import fs from "fs";
import path from "path";

export interface AgentManifest {
  id: string;
  constitutional_name: string;
  former_name: string;
  repository_id: string;
  version: string;
  constitution_version: string;
  repository_type: string;
  domain: string;
  primary_mission: string;
  capabilities: string[];
  security_level: string;
  memory_namespace: string;
  channels: string[];
  dependencies: string[];
  lifecycle_status: string;
}

export const CONSTITUTIONAL_PRINCIPLES = [
  "Everything is an Agent.",
  "Everything Communicates.",
  "Memory First: retrieve before generating.",
  "Research Before Assumption.",
  "Everything Evolves.",
  "Nothing Dies Without Leaving Knowledge.",
  "Security by Design.",
  "Scalability Without Redesign.",
] as const;

export const ENGINEERING_RULES = [
  "No duplicate implementations.",
  "No hardcoded knowledge.",
  "No repository-specific hacks.",
  "No circular dependencies.",
  "No undocumented APIs.",
  "No untracked optimization.",
  "No silent failures.",
  "No hidden prompts.",
  "No unlogged agent creation.",
] as const;

export const CONSTITUTIONAL_OATH =
  "I exist to solve a defined problem with discipline, transparency, and continuous improvement. I shall preserve knowledge, seek evidence before conclusions, collaborate with specialists, learn from every success and failure, respect the architecture of the ecosystem, and contribute to the collective intelligence of all present and future agents.";

export const REPOSITORY_FOLDERS = [
  "Chronicle",
  "Oracle",
  "Nexus",
  "Sentinel",
  "Pulse",
  "Atlas",
  "Forge",
  "Genesis",
  "Aegis",
] as const;

export function loadAgentManifests(rootDir: string): Record<string, AgentManifest> {
  const manifests: Record<string, AgentManifest> = {};

  for (const folder of REPOSITORY_FOLDERS) {
    const manifestPath = path.join(rootDir, folder, "repository.json");
    const id = folder.toLowerCase();
    if (!fs.existsSync(manifestPath)) {
      continue;
    }
    try {
      const raw = JSON.parse(fs.readFileSync(manifestPath, "utf-8"));
      manifests[id] = {
        id,
        constitutional_name: String(raw.constitutional_name || folder),
        former_name: String(raw.former_name || folder),
        repository_id: String(raw.repository_id || `${id}-1.0.0`),
        version: String(raw.version || "1.0.0"),
        constitution_version: String(raw.constitution_version || "1.0.0"),
        repository_type: String(raw.repository_type || ""),
        domain: String(raw.domain || ""),
        primary_mission: String(raw.primary_mission || ""),
        capabilities: Array.isArray(raw.capabilities) ? raw.capabilities.map(String) : [],
        security_level: String(raw.security_level || "standard"),
        memory_namespace: String(raw.memory_namespace || `${id}_memory`),
        channels: Array.isArray(raw.channels) ? raw.channels.map(String) : [],
        dependencies: Array.isArray(raw.dependencies) ? raw.dependencies.map(String) : [],
        lifecycle_status: String(raw.lifecycle_status || "deployed"),
      };
    } catch (err) {
      // Zero silent failures: surface manifest parse error in diagnostics
      console.error(`[Constitution] Failed to parse manifest at ${manifestPath}:`, err);
    }
  }

  return manifests;
}
