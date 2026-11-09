import crypto from "crypto";
import fs from "fs";
import path from "path";

export interface ChronicleStateRecord<T = any> {
  key: string;
  value: T;
  revision: number;
  updated_at_unix: number;
  sender: string;
  fingerprint: string;
}

export interface MemoryOccurrenceLog {
  date_iso: string;
  timestamp_unix: number;
  note: string;
  context?: Record<string, any>;
}

export interface ChronicleMemoryEntry {
  memory_id: string;
  concept_key: string;
  content: string;
  pillar: "episodic" | "semantic" | "procedural" | "evolutionary" | "anticipatory";
  domain: string;
  tags: string[];
  source_repository: string;
  metadata?: Record<string, any>;
  created_at_unix: number;
  last_occurred_unix: number;
  occurrence_count: number;
  occurrences: MemoryOccurrenceLog[];
  fingerprint: string;
}

export interface LearningEpisode {
  episode_id: string;
  concept_key: string;
  agent: string;
  task: string;
  success: boolean;
  outcome: string;
  error?: string | null;
  context?: Record<string, any>;
  occurrence_count: number;
  first_timestamp: number;
  timestamp: number;
  occurrences: MemoryOccurrenceLog[];
  lesson?: {
    concept_key: string;
    source: string;
    task: string;
    root_cause: string;
    lesson: string;
    adjustment: string;
    confidence: number;
    repeated: boolean;
    repeat_count: number;
    occurrences: MemoryOccurrenceLog[];
  } | null;
}

export function canonicalJson(value: any): string {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return "[" + value.map((item) => canonicalJson(item)).join(",") + "]";
  }
  const keys = Object.keys(value).sort();
  const pairs = keys
    .filter((k) => value[k] !== undefined)
    .map((k) => `${JSON.stringify(k)}:${canonicalJson(value[k])}`);
  return "{" + pairs.join(",") + "}";
}

export function sha256Hex(value: any): string {
  const payload = typeof value === "string" ? value : canonicalJson(value);
  return crypto.createHash("sha256").update(payload, "utf-8").digest("hex");
}

/**
 * Normalizes a text string into a stable concept signature by stripping volatile
 * numbers, timestamps, and hashes so recurring lessons/events consolidate into
 * a single Chronicle record instead of cluttering memory with duplicates.
 */
export function buildConceptSignature(parts: {
  source: string;
  domainOrTask: string;
  explicitConceptKey?: string;
  text: string;
}): string {
  if (parts.explicitConceptKey) {
    return `${parts.source.toLowerCase()}::${parts.domainOrTask.toLowerCase()}::${parts.explicitConceptKey.toLowerCase()}`;
  }
  const normalizedText = parts.text
    .toLowerCase()
    .replace(/\b(exp|cand|inc|ev|ep|mem|cat|cp|tr)-[a-z0-9_-]+\b/g, "<id>")
    .replace(/\d+(\.\d+)?%?/g, "<num>")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 140);
  return `${parts.source.toLowerCase()}::${parts.domainOrTask.toLowerCase()}::${sha256Hex(normalizedText).slice(0, 16)}`;
}

/**
 * ChronicleAuthoritativeStore
 *
 * Implements the Universe storage architecture:
 * - Chronicle (`Chronicle/memory/store/` & `Chronicle/memory/research/`) is the
 *   authoritative "hard drive" for the entire ecosystem.
 * - Individual agent `memory/` directories (`Oracle/memory/`, `Forge/memory/`, etc.)
 *   act strictly as bounded working RAM and write-ahead outboxes (`*.jsonl`) that
 *   flush into Chronicle.
 * - Enforces non-duplicating learning: recurring lessons or events never create
 *   duplicate rows; instead they update the existing record with "Happened again on <date>".
 */
export class ChronicleAuthoritativeStore {
  public readonly rootDir: string;
  public readonly chronicleMemoryDir: string;
  public readonly storeDir: string;
  public readonly researchDir: string;

  private readonly stateFilePath: string;
  private readonly memoriesFilePath: string;
  private readonly episodesFilePath: string;

  private stateMap: Map<string, ChronicleStateRecord> = new Map();
  private memories: ChronicleMemoryEntry[] = [];
  private episodes: LearningEpisode[] = [];

  private agentRam: Map<
    string,
    {
      handled: number;
      failed: number;
      lastActiveUnix: number | null;
      workingBuffer: Array<{
        title: string;
        source: string;
        agent: string;
        time: string;
        unix: number;
      }>;
      failureSignatures: Map<string, number>;
    }
  > = new Map();

  constructor(rootDir: string) {
    this.rootDir = rootDir;
    this.chronicleMemoryDir = path.join(rootDir, "Chronicle", "memory");
    this.storeDir = path.join(this.chronicleMemoryDir, "store");
    this.researchDir = path.join(this.chronicleMemoryDir, "research");

    fs.mkdirSync(this.storeDir, { recursive: true });
    fs.mkdirSync(this.researchDir, { recursive: true });

    this.stateFilePath = path.join(this.storeDir, "kv_state.json");
    this.memoriesFilePath = path.join(this.storeDir, "memories.json");
    this.episodesFilePath = path.join(this.storeDir, "episodes.json");

    this.loadFromDisk();
    this.consolidateExistingDuplicates();
  }

  private loadFromDisk(): void {
    if (fs.existsSync(this.stateFilePath)) {
      try {
        const parsed = JSON.parse(fs.readFileSync(this.stateFilePath, "utf-8"));
        if (parsed && typeof parsed === "object") {
          for (const [k, v] of Object.entries(parsed)) {
            this.stateMap.set(k, v as ChronicleStateRecord);
          }
        }
      } catch (err) {
        console.error("[Chronicle] Failed to load kv_state.json:", err);
      }
    }

    if (fs.existsSync(this.memoriesFilePath)) {
      try {
        const parsed = JSON.parse(fs.readFileSync(this.memoriesFilePath, "utf-8"));
        if (Array.isArray(parsed)) {
          this.memories = parsed;
        }
      } catch (err) {
        console.error("[Chronicle] Failed to load memories.json:", err);
      }
    }

    if (fs.existsSync(this.episodesFilePath)) {
      try {
        const parsed = JSON.parse(fs.readFileSync(this.episodesFilePath, "utf-8"));
        if (Array.isArray(parsed)) {
          this.episodes = parsed;
        }
      } catch (err) {
        console.error("[Chronicle] Failed to load episodes.json:", err);
      }
    }
  }

  /**
   * Consolidates any historical duplicate memories or episodes by concept_key so
   * Chronicle never looks cluttered with repeated entries of the same lesson.
   */
  private consolidateExistingDuplicates(): void {
    let changedMemories = false;
    const consolidatedMemMap = new Map<string, ChronicleMemoryEntry>();

    for (const m of this.memories) {
      const cKey =
        m.concept_key ||
        buildConceptSignature({
          source: m.source_repository || "system",
          domainOrTask: m.domain || "general",
          text: m.content || "",
        });
      const ts = m.last_occurred_unix || m.created_at_unix || Date.now() / 1000;
      const dateIso = new Date(ts * 1000).toISOString();

      if (!consolidatedMemMap.has(cKey)) {
        consolidatedMemMap.set(cKey, {
          ...m,
          concept_key: cKey,
          last_occurred_unix: ts,
          occurrence_count: m.occurrence_count || 1,
          occurrences: Array.isArray(m.occurrences) && m.occurrences.length > 0
            ? m.occurrences
            : [{ date_iso: dateIso, timestamp_unix: ts, note: m.content }],
        });
      } else {
        changedMemories = true;
        const existing = consolidatedMemMap.get(cKey)!;
        existing.occurrence_count += m.occurrence_count || 1;
        if (ts > existing.last_occurred_unix) {
          existing.last_occurred_unix = ts;
          existing.content = m.content;
        }
        const incomingOcc = Array.isArray(m.occurrences) && m.occurrences.length > 0
          ? m.occurrences
          : [{ date_iso: dateIso, timestamp_unix: ts, note: `Happened again on ${dateIso.slice(0, 16).replace("T", " ")} UTC: ${m.content}` }];
        existing.occurrences = [...existing.occurrences, ...incomingOcc].slice(-25);
      }
    }

    if (changedMemories || this.memories.some((m) => !m.concept_key)) {
      this.memories = Array.from(consolidatedMemMap.values()).sort(
        (a, b) => b.last_occurred_unix - a.last_occurred_unix
      );
      this.persistMemories();
    }

    let changedEpisodes = false;
    const consolidatedEpMap = new Map<string, LearningEpisode>();

    for (const ep of this.episodes) {
      const cKey =
        ep.concept_key ||
        buildConceptSignature({
          source: ep.agent || "system",
          domainOrTask: ep.task || "task",
          explicitConceptKey: `${ep.task}::${ep.success ? "pass" : "fail"}`,
          text: ep.lesson?.root_cause || ep.outcome || "",
        });
      const ts = ep.timestamp || Date.now() / 1000;
      const dateIso = new Date(ts * 1000).toISOString();

      if (!consolidatedEpMap.has(cKey)) {
        const occs = Array.isArray(ep.occurrences) && ep.occurrences.length > 0
          ? ep.occurrences
          : [{ date_iso: dateIso, timestamp_unix: ts, note: ep.outcome }];
        consolidatedEpMap.set(cKey, {
          ...ep,
          concept_key: cKey,
          occurrence_count: ep.occurrence_count || 1,
          first_timestamp: ep.first_timestamp || ts,
          occurrences: occs,
          lesson: ep.lesson
            ? {
                ...ep.lesson,
                concept_key: cKey,
                repeat_count: ep.lesson.repeat_count || ep.occurrence_count || 1,
                repeated: (ep.lesson.repeat_count || 1) > 1,
                occurrences: ep.lesson.occurrences || occs,
              }
            : null,
        });
      } else {
        changedEpisodes = true;
        const existing = consolidatedEpMap.get(cKey)!;
        existing.occurrence_count += ep.occurrence_count || 1;
        if (ts >= existing.timestamp) {
          existing.timestamp = ts;
          existing.outcome = ep.outcome;
        }
        const newOcc: MemoryOccurrenceLog = {
          date_iso: dateIso,
          timestamp_unix: ts,
          note: `Happened again on ${dateIso.slice(0, 16).replace("T", " ")} UTC: ${ep.outcome}`,
        };
        existing.occurrences = [...existing.occurrences, newOcc].slice(-25);
        if (existing.lesson) {
          existing.lesson.repeat_count = existing.occurrence_count;
          existing.lesson.repeated = existing.occurrence_count > 1;
          existing.lesson.occurrences = existing.occurrences;
        }
      }
    }

    if (changedEpisodes || this.episodes.some((e) => !e.concept_key)) {
      this.episodes = Array.from(consolidatedEpMap.values()).sort(
        (a, b) => b.timestamp - a.timestamp
      );
      this.persistEpisodes();
    }
  }

  private persistState(): void {
    const obj: Record<string, ChronicleStateRecord> = {};
    for (const [k, v] of this.stateMap.entries()) {
      obj[k] = v;
    }
    const tmp = `${this.stateFilePath}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(obj, null, 2) + "\n", "utf-8");
    fs.renameSync(tmp, this.stateFilePath);
  }

  private persistMemories(): void {
    const tmp = `${this.memoriesFilePath}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(this.memories, null, 2) + "\n", "utf-8");
    fs.renameSync(tmp, this.memoriesFilePath);
  }

  private persistEpisodes(): void {
    const tmp = `${this.episodesFilePath}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(this.episodes, null, 2) + "\n", "utf-8");
    fs.renameSync(tmp, this.episodesFilePath);
  }

  public ensureAgentRam(agentId: string) {
    const id = agentId.toLowerCase();
    if (!this.agentRam.has(id)) {
      this.agentRam.set(id, {
        handled: 0,
        failed: 0,
        lastActiveUnix: null,
        workingBuffer: [],
        failureSignatures: new Map(),
      });
    }
    return this.agentRam.get(id)!;
  }

  // ── Compare-and-Set State Store (Chronicle Hard Drive) ────────────────────
  public stateGet(key: string): {
    status: "complete";
    found: boolean;
    key: string;
    value: any;
    revision: number;
    updated_at_unix: number | null;
    fingerprint: string | null;
  } {
    const rec = this.stateMap.get(key);
    if (!rec) {
      return {
        status: "complete",
        found: false,
        key,
        value: null,
        revision: 0,
        updated_at_unix: null,
        fingerprint: null,
      };
    }
    return {
      status: "complete",
      found: true,
      key,
      value: JSON.parse(JSON.stringify(rec.value)),
      revision: rec.revision,
      updated_at_unix: rec.updated_at_unix,
      fingerprint: rec.fingerprint,
    };
  }

  public stateSet(
    key: string,
    value: any,
    options: { expectedRevision?: number; sender?: string } = {}
  ): {
    status: "complete" | "conflict" | "error";
    key: string;
    revision: number;
    fingerprint?: string;
    message?: string;
  } {
    const existing = this.stateMap.get(key);
    const currentRevision = existing ? existing.revision : 0;
    if (
      options.expectedRevision !== undefined &&
      options.expectedRevision !== currentRevision
    ) {
      return {
        status: "conflict",
        key,
        revision: currentRevision,
        message: `Revision mismatch for key '${key}': expected ${options.expectedRevision}, actual ${currentRevision}`,
      };
    }

    const nextRevision = currentRevision + 1;
    const nowUnix = Date.now() / 1000;
    const fingerprint = sha256Hex(value);
    const record: ChronicleStateRecord = {
      key,
      value: JSON.parse(JSON.stringify(value)),
      revision: nextRevision,
      updated_at_unix: nowUnix,
      sender: options.sender || "system",
      fingerprint,
    };
    this.stateMap.set(key, record);
    this.persistState();

    return {
      status: "complete",
      key,
      revision: nextRevision,
      fingerprint,
    };
  }

  public listStateByPrefix(prefix: string): ChronicleStateRecord[] {
    const results: ChronicleStateRecord[] = [];
    for (const [k, v] of this.stateMap.entries()) {
      if (k.startsWith(prefix)) {
        results.push(JSON.parse(JSON.stringify(v)));
      }
    }
    return results.sort((a, b) => b.updated_at_unix - a.updated_at_unix);
  }

  // ── Non-Duplicating Episodic, Semantic & Evolutionary Memory Store ─────────
  public storeMemory(entry: {
    content: string;
    pillar: ChronicleMemoryEntry["pillar"];
    domain: string;
    tags?: string[];
    source_repository: string;
    concept_key?: string;
    memory_id?: string;
    metadata?: Record<string, any>;
  }): {
    status: "complete";
    memory_id: string;
    deduplicated: boolean;
    occurrence_count: number;
  } {
    const cKey = buildConceptSignature({
      source: entry.source_repository,
      domainOrTask: entry.domain,
      explicitConceptKey: entry.concept_key,
      text: entry.content,
    });

    const nowUnix = Date.now() / 1000;
    const dateIso = new Date(nowUnix * 1000).toISOString();
    const humanDate = `${dateIso.slice(0, 10)} ${dateIso.slice(11, 19)} UTC`;

    const existingIdx = this.memories.findIndex(
      (m) =>
        m.concept_key === cKey ||
        (entry.memory_id && m.memory_id === entry.memory_id)
    );

    if (existingIdx >= 0) {
      const existing = this.memories[existingIdx];
      existing.occurrence_count = (existing.occurrence_count || 1) + 1;
      existing.last_occurred_unix = nowUnix;
      existing.content = entry.content;
      existing.metadata = {
        ...(existing.metadata || {}),
        ...(entry.metadata || {}),
        last_recurrence_note: `Happened again on ${humanDate}: ${entry.content}`,
      };
      if (!Array.isArray(existing.occurrences)) {
        existing.occurrences = [];
      }
      existing.occurrences.push({
        date_iso: dateIso,
        timestamp_unix: nowUnix,
        note: `Happened again on ${humanDate}: ${entry.content}`,
        context: entry.metadata,
      });
      if (existing.occurrences.length > 30) {
        existing.occurrences = existing.occurrences.slice(-30);
      }

      // Move updated memory to top of recency list without creating a duplicate
      this.memories.splice(existingIdx, 1);
      this.memories.unshift(existing);
      this.persistMemories();

      return {
        status: "complete",
        memory_id: existing.memory_id,
        deduplicated: true,
        occurrence_count: existing.occurrence_count,
      };
    }

    const fp = sha256Hex({ cKey, content: entry.content });
    const memoryId = entry.memory_id || `mem-${sha256Hex(cKey).slice(0, 16)}`;
    const record: ChronicleMemoryEntry = {
      memory_id: memoryId,
      concept_key: cKey,
      content: entry.content,
      pillar: entry.pillar,
      domain: entry.domain,
      tags: entry.tags || [],
      source_repository: entry.source_repository.toLowerCase(),
      metadata: entry.metadata || {},
      created_at_unix: nowUnix,
      last_occurred_unix: nowUnix,
      occurrence_count: 1,
      occurrences: [
        {
          date_iso: dateIso,
          timestamp_unix: nowUnix,
          note: `First recorded on ${humanDate}: ${entry.content}`,
          context: entry.metadata,
        },
      ],
      fingerprint: fp,
    };

    this.memories.unshift(record);
    if (this.memories.length > 2000) {
      this.memories.pop();
    }
    this.persistMemories();
    return {
      status: "complete",
      memory_id: memoryId,
      deduplicated: false,
      occurrence_count: 1,
    };
  }

  public retrieveMemories(
    filter: {
      source_repository?: string;
      domain?: string;
      pillar?: string;
      tag?: string;
      query?: string;
      limit?: number;
    } = {}
  ): ChronicleMemoryEntry[] {
    const limit = filter.limit ?? 25;
    const q = (filter.query || "").trim().toLowerCase();
    return this.memories
      .filter((m) => {
        if (
          filter.source_repository &&
          m.source_repository !== filter.source_repository.toLowerCase()
        ) {
          return false;
        }
        if (filter.domain && m.domain !== filter.domain) {
          return false;
        }
        if (filter.pillar && m.pillar !== filter.pillar) {
          return false;
        }
        if (filter.tag && !m.tags.includes(filter.tag)) {
          return false;
        }
        if (q) {
          const hay = `${m.content} ${m.domain} ${m.tags.join(" ")}`.toLowerCase();
          return hay.includes(q);
        }
        return true;
      })
      .slice(0, limit);
  }

  // ── Process-Based Non-Duplicating Episodic Learning Engine ─────────────────
  public recordEpisode(params: {
    agent: string;
    task: string;
    success: boolean;
    outcome: string;
    concept_key?: string;
    error?: string | null;
    context?: Record<string, any>;
    customLesson?: {
      root_cause: string;
      lesson: string;
      adjustment: string;
      confidence: number;
    };
  }): LearningEpisode {
    const agentId = params.agent.toLowerCase();
    const ram = this.ensureAgentRam(agentId);
    const nowUnix = Date.now() / 1000;
    const dateIso = new Date(nowUnix * 1000).toISOString();
    const humanDate = `${dateIso.slice(0, 10)} ${dateIso.slice(11, 19)} UTC`;
    ram.lastActiveUnix = nowUnix;

    const cKey = buildConceptSignature({
      source: agentId,
      domainOrTask: params.task,
      explicitConceptKey:
        params.concept_key || `${params.task}::${params.success ? "pass" : "fail"}`,
      text: params.customLesson?.root_cause || params.outcome,
    });

    if (params.success) {
      ram.handled += 1;
    } else {
      ram.failed += 1;
    }

    const sigCount = (ram.failureSignatures.get(cKey) || 0) + 1;
    ram.failureSignatures.set(cKey, sigCount);

    // Check if an episode with this concept_key already exists in Chronicle
    const existingIdx = this.episodes.findIndex((e) => e.concept_key === cKey);

    ram.workingBuffer.unshift({
      title: `${params.task}: ${params.outcome}`,
      source: params.success ? "Chronicle Verified" : "Diagnostic Blocker",
      agent: agentId,
      time: dateIso.slice(11, 19) + " UTC",
      unix: nowUnix,
    });
    if (ram.workingBuffer.length > 25) {
      ram.workingBuffer.pop();
    }

    if (existingIdx >= 0) {
      const existing = this.episodes[existingIdx];
      existing.occurrence_count = (existing.occurrence_count || 1) + 1;
      existing.timestamp = nowUnix;
      existing.outcome = params.outcome;
      existing.context = { ...(existing.context || {}), ...(params.context || {}) };

      const recurrenceEntry: MemoryOccurrenceLog = {
        date_iso: dateIso,
        timestamp_unix: nowUnix,
        note: `Happened again on ${humanDate}: ${params.outcome}`,
        context: params.context,
      };
      if (!Array.isArray(existing.occurrences)) {
        existing.occurrences = [];
      }
      existing.occurrences.push(recurrenceEntry);
      if (existing.occurrences.length > 30) {
        existing.occurrences = existing.occurrences.slice(-30);
      }

      if (existing.lesson) {
        existing.lesson.lesson = params.customLesson?.lesson || existing.lesson.lesson;
        existing.lesson.adjustment =
          params.customLesson?.adjustment || existing.lesson.adjustment;
        existing.lesson.repeat_count = existing.occurrence_count;
        existing.lesson.repeated = true;
        existing.lesson.confidence = Math.min(
          0.98,
          Number((existing.lesson.confidence + 0.03).toFixed(2))
        );
        existing.lesson.occurrences = existing.occurrences;
      }

      this.episodes.splice(existingIdx, 1);
      this.episodes.unshift(existing);
      this.persistEpisodes();

      if (existing.lesson) {
        this.storeMemory({
          concept_key: cKey,
          content: `[Recurring Lesson x${existing.occurrence_count} — Last on ${humanDate}] ${existing.lesson.lesson} (Adjustment: ${existing.lesson.adjustment})`,
          pillar: "evolutionary",
          domain: "learning",
          tags: ["lesson", agentId, params.task, "recurring"],
          source_repository: agentId,
          metadata: {
            episode_id: existing.episode_id,
            occurrence_count: existing.occurrence_count,
            last_occurred: humanDate,
            occurrences: existing.occurrences.slice(-8),
          },
        });
      }

      return existing;
    }

    // First time this concept occurred
    const initialOcc: MemoryOccurrenceLog = {
      date_iso: dateIso,
      timestamp_unix: nowUnix,
      note: `First recorded on ${humanDate}: ${params.outcome}`,
      context: params.context,
    };

    const lessonObj: LearningEpisode["lesson"] = params.customLesson
      ? {
          concept_key: cKey,
          source: "empirical_experiment",
          task: params.task,
          root_cause: params.customLesson.root_cause,
          lesson: params.customLesson.lesson,
          adjustment: params.customLesson.adjustment,
          confidence: params.customLesson.confidence,
          repeated: false,
          repeat_count: 1,
          occurrences: [initialOcc],
        }
      : !params.success
      ? {
          concept_key: cKey,
          source: "constitutional_reflection",
          task: params.task,
          root_cause: params.error || params.outcome || "unverified_precondition",
          lesson: `Task '${params.task}' blocked/failed (${params.error || params.outcome}). Preconditions must be verified in Chronicle before execution.`,
          adjustment:
            "Verify Chronicle dataset manifest, broker cost profile, and schema constraints before retrying.",
          confidence: 0.65,
          repeated: false,
          repeat_count: 1,
          occurrences: [initialOcc],
        }
      : null;

    const ep: LearningEpisode = {
      episode_id: `ep-${sha256Hex(cKey).slice(0, 12)}`,
      concept_key: cKey,
      agent: agentId,
      task: params.task,
      success: params.success,
      outcome: params.outcome,
      error: params.error || null,
      context: params.context || {},
      occurrence_count: 1,
      first_timestamp: nowUnix,
      timestamp: nowUnix,
      occurrences: [initialOcc],
      lesson: lessonObj,
    };

    this.episodes.unshift(ep);
    if (this.episodes.length > 1000) {
      this.episodes.pop();
    }
    this.persistEpisodes();

    if (lessonObj) {
      this.storeMemory({
        concept_key: cKey,
        content: `Lesson [${agentId}/${params.task}]: ${lessonObj.lesson} (Root cause: ${lessonObj.root_cause} | Adjustment: ${lessonObj.adjustment})`,
        pillar: "evolutionary",
        domain: "learning",
        tags: ["lesson", agentId, params.task],
        source_repository: agentId,
        metadata: {
          episode_id: ep.episode_id,
          occurrence_count: 1,
          occurrences: [initialOcc],
        },
      });
    }

    return ep;
  }

  /**
   * Memory First: Retrieve prior deduplicated lessons for an agent & task BEFORE executing.
   */
  public adviceFor(
    agent: string,
    task: string
  ): Array<NonNullable<LearningEpisode["lesson"]>> {
    const agentId = agent.toLowerCase();
    const taskParts = new Set(task.toLowerCase().split("."));
    const matching: Array<NonNullable<LearningEpisode["lesson"]>> = [];

    for (const ep of this.episodes) {
      if (ep.agent !== agentId || !ep.lesson) continue;
      const epParts = ep.lesson.task.toLowerCase().split(".");
      const overlaps = epParts.some((p) => taskParts.has(p));
      if (ep.lesson.task === task || overlaps) {
        matching.push(ep.lesson);
      }
      if (matching.length >= 8) break;
    }

    return matching;
  }

  public getAgentRamSnapshot(agentId: string) {
    const id = agentId.toLowerCase();
    const ram = this.ensureAgentRam(id);
    const agentFolder = id.charAt(0).toUpperCase() + id.slice(1);
    const memoryDir = path.join(this.rootDir, agentFolder, "memory");

    const localFiles: Array<{ file: string; bytes: number; modified_at: string }> = [];
    if (fs.existsSync(memoryDir)) {
      const walk = (dir: string, prefix = "") => {
        for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
          const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
          const full = path.join(dir, entry.name);
          if (entry.isDirectory()) {
            walk(full, rel);
          } else if (entry.isFile() && !entry.name.startsWith(".")) {
            const st = fs.statSync(full);
            localFiles.push({
              file: rel,
              bytes: st.size,
              modified_at: st.mtime.toISOString(),
            });
          }
        }
      };
      walk(memoryDir);
    }

    const agentEpisodes = this.episodes.filter((e) => e.agent === id);
    const successes = agentEpisodes.filter((e) => e.success).length;
    const failures = agentEpisodes.length - successes;
    const lessons = agentEpisodes.filter((e) => e.lesson !== null).length;

    return {
      agent: id,
      ram_handled: ram.handled,
      ram_failed: ram.failed,
      total_episodes_in_chronicle: agentEpisodes.length,
      chronicle_successes: successes,
      chronicle_failures: failures,
      lessons_learned: lessons,
      last_active_unix: ram.lastActiveUnix,
      working_buffer: ram.workingBuffer,
      local_ram_files: localFiles,
      unadapted_failures: Array.from(ram.failureSignatures.entries())
        .filter(([, count]) => count >= 3)
        .map(([signature, count]) => ({ signature, count })),
    };
  }

  public getChronicleOverview() {
    let diskBytes = 0;
    const countBytes = (dir: string) => {
      if (!fs.existsSync(dir)) return;
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) countBytes(full);
        else if (entry.isFile()) diskBytes += fs.statSync(full).size;
      }
    };
    countBytes(this.chronicleMemoryDir);

    const totalRecurrencesConsolidated = this.memories.reduce(
      (sum, m) => sum + Math.max(0, (m.occurrence_count || 1) - 1),
      0
    );

    return {
      state_keys_count: this.stateMap.size,
      memories_count: this.memories.length,
      episodes_count: this.episodes.length,
      lessons_count: this.episodes.filter((e) => e.lesson !== null).length,
      recurrences_consolidated: totalRecurrencesConsolidated,
      storage_bytes: diskBytes,
      storage_formatted:
        diskBytes >= 1024 * 1024
          ? `${(diskBytes / (1024 * 1024)).toFixed(2)} MB`
          : `${(diskBytes / 1024).toFixed(1)} KB`,
      recent_memories: this.memories.slice(0, 12),
      recent_episodes: this.episodes.slice(0, 12),
    };
  }
}
