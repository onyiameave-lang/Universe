# Forge V2: Constitutional Strategy Evolution & Chronicle-Backed Learning Engine

This plan implements the **Forge V2 Implementation Plan** specification PDF under strict enforcement of the **Universe Constitutional Law (`shared/constitutional.py` & `STORAGE_LAYOUT.md`)**: **zero hardcoded data or knowledge**, **Chronicle as the sole authoritative persistent store ("hard drive") with agent `memory/` directories acting strictly as bounded RAM and durable outboxes**, **process-driven episodic learning**, and **zero hidden faults or silent fallbacks**.

---

### User Review & Constitutional Mandates Confirmed

> [!IMPORTANT]
> The following constitutional mandates govern every module we build or touch:

- **Law 1 — Zero Hardcoding (`No hardcoded knowledge`)**:
  - Remove all static/seeded metrics, hardcoded strategy records, and fabricated feed items from the runtime server.
  - All agent definitions, capabilities, and missions are loaded dynamically from each repository's `repository.json` manifest and `Chronicle/memory/`. If no experiments or trades have run yet, metrics and registries reflect the exact ground-truth state (`0` runs, `UNAVAILABLE`, or the actual persisted records read from Chronicle).
- **Law 2 — Chronicle as Authoritative Hard Drive; Agent Memory as RAM / Outbox (`Memory First`)**:
  - **Chronicle (`Chronicle/memory/store/` & `Chronicle/memory/research/`)** is the single source of truth for all durable state: strategy catalogs (`oracle.strategy_catalog.snapshot.v1`), frozen V1 incumbent snapshots, 7-timeframe OHLCV manifests, verified broker cost profiles, Forge experiment ledgers, learned timeframe policies, ablation screen lessons, and candidate governance records (`state.get` / `state.set` with optimistic `expected_revision` concurrency control).
  - **Agent-local `memory/` (`Forge/memory/`, `Oracle/memory/`, etc.)** acts strictly as bounded working RAM and durable write-ahead outboxes (`forge_bridge_outbox.jsonl`, `knowledge_outbox`) that flush and synchronize into Chronicle via `ChronicleSync`.
- **Law 3 — Learning as an Empirical Process (`Everything Evolves` & `Nothing Dies Without Leaving Knowledge`)**:
  - Forge and Oracle do not receive pre-baked answers. Before executing any task, an agent queries Chronicle (`advice_for(task)`) for prior lessons and ablation rewards in that scope. Every experiment, trade outcome, or failure records an `Episode`, derives a structured lesson (with empirical reward/win-rate delta and idempotency via `seen_experiment_ids`), and commits that lesson back to Chronicle so subsequent runs select targets based on accumulated evidence.
- **Law 4 — Zero Hidden Faults (`No silent failures`)**:
  - Every missing precondition—unverified broker cost profile, missing OHLC price basis, insufficient bar lookback, missing Chronicle revision, or unconfigured bridge token—surfaces immediately as an explicit, structured `BLOCKED`, `UNAVAILABLE`, or `ERROR` record with the exact diagnostic reason. Nothing is ever masked with `0` cost defaults or mock responses.

---

### 1. Overview & Core Concept

- **What It Does**:
  - Replaces static runtime responses with a live, file-and-Chronicle-backed execution engine that reads the actual repository manifests (`*/repository.json`), Chronicle research/state stores (`Chronicle/memory/`), and agent RAM outboxes (`Oracle/memory/`, `Forge/memory/`).
  - Implements **Universal Forge Core** (domain-agnostic experiment contracts, hypothesis queue, walk-forward + untouched lockbox evaluator, paired-bootstrap challenger benchmark, and 7-state candidate governance registry) backed by Chronicle state keys (`forge.oracle.*`) and local working caches.
  - Implements the **Oracle Training Adapter** and **Section 12 Pilot (Candidates A–E)** so Forge learns across **Stop-Loss** (§3.1), **Take-Profit/Exit** (§3.2), **Constant-Monetary-Risk Position Sizing** (§3.3), and **Contextual 7-Timeframe Policies** (§4), generating structured **Trade Theses** (§5.1) and **No-Trade Explanations** (§5.2) and exporting approved candidates via the **Oracle V1 Compatibility Adapter** (§10.1).
- **Target Audience / Persona**: Universe AI Ecosystem architect and quantitative reviewer auditing Forge V2 and Chronicle state transitions.
- **Key Value**: Guarantees 100% constitutional alignment—every number, strategy candidate, lesson, and blocker on screen and in the API is derived from real code execution and persisted Chronicle records.

---

### 2. User Experience & Visual Design

- **Key User Flows**:
  1. **Constitutional Ground-Truth Telemetry**: Every agent page (`index.html`, `forge.html`, `oracle.html`, `chronicle.html`, etc.) displays real counts and records read from `Chronicle/memory/` and the agent's local RAM/outbox state—no hardcoded numbers or synthetic placeholders.
  2. **Chronicle Hard-Drive vs. Agent RAM Inspector**: On the Forge and Chronicle dashboards, operators can inspect what is currently held in **Agent RAM / Outbox** (pending events, active working episodes, cached models) versus what is committed to **Chronicle Persistent Storage** (versioned state keys, dataset manifests, cost profiles, hypothesis evidence, and learned lessons).
  3. **Controlled Section 12 Pilot Execution (Candidates A–E)**:
     - Select a symbol and frozen incumbent from Chronicle's synced V1 catalog (`Chronicle/memory/research/oracle_strategy_catalog/`).
     - Pre-flight check validates the 7-timeframe OHLCV manifest and verified broker cost profile in Chronicle. If any required input is absent, the run halts in an explicit **`BLOCKED`** state showing the exact missing artifact.
     - When preconditions pass, Forge executes Candidates **A (Baseline)**, **B (Structure Stop/Target)**, **C (ATR Stop/Target)**, **D (Hybrid)**, and **E (Constant-Risk Sizing)** across chronological development, walk-forward, and untouched lockbox partitions, recording every learned lesson and timeframe policy back into Chronicle.
  4. **7-State Governance & V1 Compatibility Export**: Candidates advance through `DISCOVERED → EXPERIMENTING → EVALUATED → VALIDATED → CERTIFIED → APPROVED FOR DEPLOYMENT → DEPLOYED` via explicit governance transitions stored in Chronicle, with validated V1 `StrategyGenome` export bundles generated on approval.
- **Visual Identity & Theme**:
  - Preserves the Universe dark/light cosmos stylesheet (`frontend/shared/shared.css`, `components.css`) with tabular monospace numerals (`tabular-nums`) and explicit semantic state labels (`BLOCKED`, `UNAVAILABLE`, `DISCOVERED`, `EXPERIMENTING`, `EVALUATED`, `VALIDATED`, `CERTIFIED`, `APPROVED FOR DEPLOYMENT`, `DEPLOYED`).

---

### 3. Key Product Decisions & Trade-Offs

- **Decision 1: Chronicle State Store as the Single Source of Truth (`Chronicle/memory/store` & `Chronicle/memory/research`)**
  - *Chosen Approach*: Implement the compare-and-set Chronicle state store (`state.get`, `state.set` with `expected_revision`, `memory.store`, `memory.retrieve`) persisted on disk under `Chronicle/memory/`, while agent `memory/` folders hold only local RAM caches and write-ahead outboxes (`forge_bridge_outbox.jsonl`, `knowledge_outbox.jsonl`).
  - *Why*: Directly enforces the user's architecture where Chronicle is the ecosystem's hard drive and individual agent memory directories act as RAM.
- **Decision 2: Process-Driven Learning Loop (`advice_for` → `execute` → `record_episode` → `store_memory`)**
  - *Chosen Approach*: Before Forge selects an ablation target or timeframe policy, it queries Chronicle (`forge.oracle.ablation_learning.<scope_hash>`) for accumulated screen rewards and failure lessons. After each experiment completes, Forge updates the Chronicle learning record idempotently (`seen_experiment_ids`) so future runs adapt from empirical outcomes.
  - *Why*: Ensures the agent genuinely learns over time from data and experiments rather than relying on static heuristics.

---

### 4. Technical Architecture & Data Strategy *(Technical Reference)*

- **Architecture & Component Diagram**:

```
┌───────────────────────────────────────────────────────────────────────────────────┐
│                   AGENT WORKING MEMORY (RAM & Write-Ahead Outboxes)               │
│  ┌────────────────────────────────────┐   ┌────────────────────────────────────┐  │
│  │ Oracle RAM (`Oracle/memory/`)      │   │ Forge RAM (`Forge/memory/`)        │  │
│  │ • Working strategy cache           │   │ • Active experiment working state  │  │
│  │ • `forge_bridge_outbox.jsonl`      │   │ • `knowledge_outbox.jsonl`         │  │
│  └─────────────────┬──────────────────┘   └─────────────────┬──────────────────┘  │
└────────────────────┼────────────────────────────────────────┼─────────────────────┘
                     │ Durable Outbox Flush & Compare-and-Set │
                     ▼                                        ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│              CHRONICLE AUTHORITATIVE HARD DRIVE (`Chronicle/memory/`)             │
│  • `store/` — Revisioned KV State (`forge.oracle.*`, hypotheses, holdout locks)   │
│  • `store/` — Episodic, Semantic & Evolutionary Lessons (`advice_for` retrieval)  │
│  • `research/oracle_strategy_catalog/` — Content-Addressed V1 Catalog Snapshots   │
│  • `research/oracle_incumbent_baselines/` — Frozen V1 Incumbent Baselines         │
│  • `research/oracle_market_data/` — 7-Timeframe OHLCV Bundles & Manifests         │
│  • `research/oracle_cost_profiles/` — Verified Broker Cost Profiles               │
│  • `research/oracle_candidates/` — 7-State Candidate Governance & V1 Exports      │
└─────────────────────────────────────────┬─────────────────────────────────────────┘
                                          ▲
                                          │ Reads Prior Lessons & Commits Evidence
                                          ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│                UNIVERSAL FORGE CORE + ORACLE TRAINING ADAPTER                     │
│  1. Pre-Flight Gate: Verifies Chronicle Manifest + Cost Profile (or -> BLOCKED)   │
│  2. Memory First: Retrieves prior Chronicle ablation & failure lessons            │
│  3. Controlled Pilot Runner: Executes Candidates A, B, C, D, E on 7-TF Data       │
│  4. Explainability Engine: Emits Per-Trade Thesis & 8 No-Trade Categories         │
│  5. Walk-Forward & Lockbox Validator + Paired Bootstrap Challenger Benchmark      │
│  6. Governance & V1 Adapter: Certifies & Exports Validated V1 StrategyGenome      │
└───────────────────────────────────────────────────────────────────────────────────┘
```

- **Data Model & State**:
  - **Chronicle Revisioned State Record**: `{ key, value, revision, updated_at_unix, source_repository, content_fingerprint }`.
  - **Chronicle Episodic/Evolutionary Lesson**: `{ memory_id, agent, task, root_cause, lesson, adjustment, qualified_screens, reward_sum, seen_experiment_ids, timestamp }`.
  - **Forge Candidate Governance Artifact**: `{ candidate_id, experiment_id, symbol, regime, variant ('A_baseline' | 'B_structure' | 'C_atr' | 'D_hybrid' | 'E_sizing'), lifecycle_state ('DISCOVERED' | 'EXPERIMENTING' | 'EVALUATED' | 'VALIDATED' | 'CERTIFIED' | 'APPROVED_FOR_DEPLOYMENT' | 'DEPLOYED' | 'REJECTED' | 'BLOCKED'), contract, parameter_diffs, trade_theses, no_trade_counts, development_metrics, walk_forward_report, lockbox_report, bootstrap_comparison, v1_compatibility_export }`.
