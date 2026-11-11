# ATLAS — Institutional Research Intelligence Desk (Frozen V1 Baseline + ATLAS V2 Institutional Engine)

> **Constitutional Role**: Book I Part IV Article VII · Book II Chapter IV ("Research Before Assumption")
> **Blueprint Specification**: `/Atlas/documentation/ATLAS_V2_INSTITUTIONAL_BLUEPRINT.md` (Full 25-Section ATLAS V2 Institutional-Grade Research Agent Blueprint)

---

## 1. Core Identity & Preservation Mandate

**Atlas V2** is an evidence-first, source-graded, method-explicit, adversarially reviewed, reproducible institutional research engine that turns complex questions into decision-ready, audit-ready intelligence for both human decision-makers and peer Universe agents (`Oracle`, `Chronicle`, `Forge`, `Sentinel`, `Pulse`, `Genesis`, `Aegis`, `Universal Coordinator`).

Per **Sections 20 & 25** of the ATLAS V2 Blueprint:
- **Frozen Atlas V1 Baseline (`ResearchEngine.investigate`, `generate_hypothesis`, `validate_hypothesis`, `synthesize`, `fetch_and_analyze`)** is preserved intact for backward compatibility, task-specific quick research, and blind benchmark comparison.
- **Atlas V2 Institutional Engine (`Atlas/core/institutional_research_v2.py`)** implements the complete 25-section institutional specification without regression risk.

---

## 2. Eight Operating Modes (Section 3.2)

| Operating Mode | Purpose | Primary Output | Evidence & Review Bar |
| :--- | :--- | :--- | :--- |
| `quick_research` | Rapid, source-grounded brief when speed matters | 1–2 page brief, key facts, source list, confidence & gaps | Tier A–C preferred; automated claim & source check |
| `deep_research` | Full institutional investigation of a complex question | 14-section Deep-Research Dossier, Evidence Ledger, Scenarios, Red-Team Memo | Primary sources + independent corroboration + full 10-audit QA |
| `continuous_monitoring` | Track a thesis, entity, market, technology, or risk over time | Delta alert, updated probabilities, invalidation status | Timestamped new evidence vs prior baseline |
| `thematic_research` | Cross-market or cross-industry structural study | Thematic report, value-chain map, signposts | Multi-domain primary + empirical data |
| `company_asset_research` | Deep dive on an issuer, asset, currency, commodity, or protocol | 12-section Investment Research Dossier, driver tree, risks, catalysts | Filings/official data + accounting/data audit + skeptic pass |
| `forensic_research` | Contradiction, fraud, governance, accounting, or claim verification | Forensic memo, timeline, claim-by-claim verification table | Primary documents + multi-source triangulation + adversarial audit |
| `technical_scientific_research` | Evaluate papers, methods, models, benchmarks, or engineering claims | Systematic technical review, replication/validity assessment | Papers, code, datasets, benchmarks + statistical audit |
| `agent_support` | Structured evidence package for `Oracle`, `Forge`, `Chronicle`, `Sentinel`, `Pulse`, `Genesis`, `Aegis` | Machine-readable `ResearchPackage` + target-specific `agent_handoff` | Schema, provenance, point-in-time cutoff & freshness check |

---

## 3. The 11-Stage Institutional Research Lifecycle (Section 4)

Every Atlas V2 investigation progresses through 11 auditable stages (`ResearchEngine.investigate_v2`):

1. **Stage 1 — Intake (`ResearchMandate`)**: Locks the exact testable question, decision context, audience, scope boundaries, units, sufficiency criteria, `as_of_timestamp`, `publication_cutoff_timestamp`, timezone (`UTC`), freshness window, and compute budget.
2. **Stage 2 — Decompose (`QuestionTreeAndHypothesisMap`)**: Breaks the root question into 4 structured sub-questions (`definitional_and_scope`, `empirical_and_historical`, `causal_mechanism`, `counterargument_and_falsification`) and competing hypotheses (`H1_LEADING`, `H2_ALTERNATIVE`, `H0_NULL`) with explicit confirming/disconfirming evidence and falsification conditions.
3. **Stage 3 — Plan (`ResearchPlan`)**: Pre-specifies analytical methods, source tier priorities, corroboration rules, denominators, inclusion/exclusion rules, and stopping criteria.
4. **Stage 4 — Acquire (`SourceRegister`)**: Collects evidence across multi-source connectors (`Wikipedia`, `arXiv`, `Semantic Scholar`, `PubMed`, `Crossref`, `HackerNews`, `GDELT`, `Web`, `PDF`, `Chronicle`) or structured point-in-time evidence feeds.
5. **Stage 5 — Validate (`EvidenceLedger`)**:
   - **5-Tier Source Hierarchy**: Grades every source into `Tier A` (Primary filings/official stats/central banks), `Tier B` (Peer-reviewed/institutional working papers), `Tier C` (Informed secondary/reputable press/preprints), `Tier D` (Sentiment/crowd/market tone — context only), or `Tier E` (Synthetic/anonymous/unverified — quarantined).
   - **Syndication & Source-Laundering Deduplication**: Clusters sources by `independence_group` so 5 sites repeating 1 wire story or press release count as **1 origin**, not 5 independent confirmations.
   - **Temporal Point-in-Time Discipline**: Tracks `published_at`, `event_effective_at`, and `retrieved_at` separately and blocks any look-ahead source published after `publication_cutoff_timestamp`.
6. **Stage 6 — Analyze (`ReproducibleWorkpapers` & `DataLineageRecord`)**: Runs domain-specific analytical playbooks (`fundamental_and_business`, `quantitative_and_statistical`, `macro_policy_and_geopolitical`, `market_and_trading`, `technology_science_and_qualitative`, `forensic_verification`), constructs the Causal Mechanism Graph (`X -> M -> Y` with confounders `Z` and colliders), and builds the 4-Scenario Probability Forecast (`base`, `upside`, `downside`, `stress` summing to `1.0000` with Brier calibration).
7. **Stage 7 — Challenge (`RedTeamMemo`)**: Independent Skeptic/Red-Team pass that formulates the strongest opposing thesis, runs a 3-point pre-mortem, checks for source concentration, look-ahead leakage, and circular validation, and recommends an explicit confidence haircut when vulnerabilities exist.
8. **Stage 8 — Review (`ReviewRecord`)**: Executes the **10 Pre-Publication Audits** (`claim_audit`, `citation_audit`, `arithmetic_audit`, `definition_audit`, `temporal_audit`, `source_audit`, `method_audit`, `counterargument_audit`, `reproducibility_audit`, `language_audit`) and scans against all **17 Failure Modes** (`FM-01` through `FM-17`).
9. **Stage 9 — Communicate (`ResearchDossier` & `ResearchPackage`)**: Produces both the **14-Section Deep-Research Dossier** (Section 17), the **12-Section Investment Research Dossier** (Section 10.2), and the machine-readable **`ResearchPackage`** (Section 18).
10. **Stage 10 — Monitor (`UpdatePlanAndChangeLog` & Knowledge Graph)**: Defines observable signposts, invalidation triggers, review cadence, and promotes only verified/corroborated claims to durable Chronicle memory while keeping unverified leads quarantined.
11. **Stage 11 — Learn (`ResearchPerformanceRecord`)**: Computes all **13 Institutional Performance Metrics** (Section 15), including `claim_support_rate`, `primary_source_coverage`, `independent_corroboration`, `citation_precision`, `contradiction_detection`, `factual_error_rate`, `reproducibility`, `forecast_calibration_error`, `research_completeness`, `time_to_defensible_answer_sec`, `update_latency_sec`, `reviewer_disagreement`, and `decision_usefulness`.

---

## 4. CLI & Programmatic Usage

### Command Line Interface (`Atlas/main.py`)
```bash
# Run Atlas V2 11-Stage Institutional Deep-Research Dossier
python3 Atlas/main.py --v2 --query "Map central bank rate differential transmission to EURUSD and USDJPY" --domain trading --mode deep_research

# Build a structured Section 19 Handoff for Forge, Oracle, Chronicle, Sentinel_Pulse, Genesis, or Aegis
python3 Atlas/main.py --handoff Forge --query "EURUSD multi-timeframe microstructure and regime hypothesis" --domain trading

# Run the Section 22 5-Assignment Pilot Programme & Frozen V1 vs V2 Benchmark Suite
python3 Atlas/main.py --pilot-benchmark

# Run Frozen Atlas V1 quick investigation
python3 Atlas/main.py --query "CRISPR off-target effects" --domain science
```

### Python API (`AtlasAgent` & `ResearchEngine`)
```python
from Atlas.agents.research_agent import AtlasAgent

agent = AtlasAgent()
agent.start()

# 1. Full 14-Section Deep-Research Dossier + 12-Section Investment Dossier + ResearchPackage
res = agent.execute("research.dossier_v2", {
    "query": "Evaluate rate differential transmission to EURUSD across volatility regimes",
    "domain": "trading",
    "operating_mode": "deep_research",
})

# 2. Structured Agent Handoff for Oracle / Forge / Chronicle / Aegis
handoff = agent.execute("research.handoff", {
    "target_agent": "Oracle",
    "query": "EURUSD macro & volatility regime context",
    "domain": "trading",
})

# 3. Pilot Benchmark Suite (5 controlled assignments including adversarial red-team test)
benchmark = agent.execute("research.pilot_benchmark", {})
```

---

## 5. Directory Structure

```text
Atlas/
├── README.md                                              # Overview & Quickstart
├── main.py                                                # CLI & interactive desk (V1 + V2)
├── agents/
│   └── research_agent.py                                  # AtlasAgent (V1 + V2 capabilities)
├── core/
│   ├── institutional_research_v2.py                       # Full 25-Section ATLAS V2 Engine
│   ├── research_engine.py                                 # Frozen V1 Engine + V2 Bridge
│   ├── sources.py                                         # Multi-source parallel connectors
│   └── chronicle_access.py                                # Bounded Chronicle memory recall
├── intelligence/
│   ├── analysis.py                                        # Relevance, claims, corroboration
│   └── contradiction.py                                   # Contradiction detection engine
└── documentation/
    └── ATLAS_V2_INSTITUTIONAL_BLUEPRINT.md                # Complete 25-Section Architecture Reference
```
