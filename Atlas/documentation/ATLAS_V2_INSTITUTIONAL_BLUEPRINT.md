# ATLAS V2: Institutional-Grade Research Agent Blueprint — Architecture & Implementation Reference

> **Source Specification**: *ATLAS V2: Institutional-Grade Research Agent Blueprint (Research doctrine • Evidence architecture • Analytical capabilities • Governance • Agent design • Implementation roadmap)*
> **Implementation Module**: `/Atlas/core/institutional_research_v2.py`
> **Engine & Agent Integration**: `/Atlas/core/research_engine.py`, `/Atlas/agents/research_agent.py`, `/Atlas/main.py`
> **Verification Suite**: `/tests/test_atlas_v2_institutional_blueprint.py`

---

## Section 1. Executive Summary & Core Identity

**Atlas V2** is the institutional research and intelligence engine of the Universe ecosystem. While **Atlas V1** remains preserved as a frozen benchmark and rapid-lookup baseline, **Atlas V2** operates as a multi-disciplinary institutional research organization—combining the analytical rigor of a top buy-side/sell-side desk, central bank policy division, investigative journalism unit, scientific systematic-review group, and quantitative forecasting team.

> **Core Identity Statement**:
> *"Atlas V2 is an evidence-first, source-graded, method-explicit, adversarially reviewed, reproducible research engine that turns complex questions into decision-ready, audit-ready intelligence."*

---

## Section 2. Institutional Benchmarks Mapped to Atlas V2 Modules

| Institutional Benchmark | Core Standard | Atlas V2 Implementing Module |
| :--- | :--- | :--- |
| **Buy-Side Fundamental & Macro Research** | Decision-oriented memos, explicit variant perception, thesis drivers, catalysts, scenario ranges, disconfirming evidence, risk implications | `MultiDomainAnalyticalEngine` (`fundamental_and_business`) + `CausalAndScenarioSpecialist` + `12-Section Investment Dossier` |
| **Sell-Side Equity, Fixed Income, FX & Commodity Research** | Transparent models, methodology notes, separation of fact vs estimate vs opinion, clean exhibits, sector/macro linkage | `MultiDomainAnalyticalEngine` + `ResearchEditorSpecialist` + `GovernanceAuditSpecialist` |
| **Central Bank, IMF, BIS, World Bank & OECD Research** | Rigorous data definitions, structural vs cyclical separation, policy transmission channels, uncertainty bands, replicable empirical appendices | `MultiDomainAnalyticalEngine` (`macro_policy_and_geopolitical`, `quantitative_and_statistical`) |
| **Elite Policy & Strategic Think Tanks** | Explicit scope, multidisciplinary synthesis, competing interpretations, peer challenge | `ResearchOrchestratorSpecialist` (`QuestionTreeAndHypothesisMap`) + `SkepticRedTeamSpecialist` |
| **Investigative & Forensic Research Desks** | Primary document verification, independent corroboration, syndication origin tracing, timeline reconstruction, source-incentive analysis | `PrimarySourceVerifierSpecialist` + `MultiDomainAnalyticalEngine` (`forensic_verification`) |
| **Scientific & Systematic Review Standards** | Pre-specified search protocols, inclusion/exclusion criteria, bias assessment, effect-size reporting, replication awareness | `ResearchPlan` + `MultiDomainAnalyticalEngine` (`technology_science_and_qualitative`) |
| **Quantitative & Forecasting Desks** | Point-in-time data integrity, out-of-sample testing, Brier/log calibration, explicit base rates, regime conditioning | `CausalAndScenarioSpecialist` (`ScenarioForecastSuite`) + `MonitoringAndMemorySpecialist` |

---

## Section 3. Mission, 8 Operating Modes & Non-Negotiable Boundaries

### 3.1 Mission
Deliver **truth-seeking, decision-useful, reproducible research** across markets, macroeconomics, companies, assets, technologies, policy, scientific literature, and operational questions for both human users and peer agents in the Universe ecosystem.

### 3.2 Operating Modes (`OPERATING_MODES`)
1. `quick_research` — Rapid, source-grounded brief with explicit confidence and gaps.
2. `deep_research` — Full 11-stage investigation producing the 14-section Deep-Research Dossier, Evidence Ledger, Scenarios, and Red-Team Memo.
3. `continuous_monitoring` — Tracks a thesis, entity, market, technology, or risk over time with delta alerts and invalidation triggers.
4. `thematic_research` — Cross-market or cross-industry structural study with transmission maps and signposts.
5. `company_asset_research` — Deep dive on an issuer, currency, commodity, or protocol producing the 12-section Investment Research Dossier.
6. `forensic_research` — Contradiction, fraud, source-laundering, governance, or claim-by-claim verification memo.
7. `technical_scientific_research` — Systematic evaluation of papers, algorithms, models, benchmarks, and replication validity.
8. `agent_support` — Machine-readable `ResearchPackage` and target-specific handoffs for `Oracle`, `Chronicle`, `Forge`, `Sentinel_Pulse`, `Genesis`, `Aegis`, and `Universal_Coordinator`.

### 3.3 Non-Negotiable Boundaries (`NON_NEGOTIABLE_BOUNDARIES`)
- Never fabricate a source, quote, number, date, citation, dataset, or consensus view.
- Never treat search snippets, headlines, social posts, or AI summaries as verified facts without checking underlying sources.
- Never present correlation as causation without a defensible causal design or explicit caveat.
- Never hide uncertainty, conflicting evidence, source dependence, or data gaps to make a conclusion look cleaner.
- Never overwrite historical claims silently; preserve point-in-time knowledge and log revisions.
- Never execute trades, place orders, or mutate external production systems unless explicitly authorized by system governance.

---

## Section 4. The Institutional 11-Stage Research Lifecycle

Implemented in `AtlasV2InstitutionalEngine.run_institutional_research()`:

| Stage | Name | Core Activity | Output Artifact |
| :---: | :--- | :--- | :--- |
| **1** | **Intake** | Clarify decision, scope, horizon, definitions, constraints, and success criteria | `ResearchMandate` |
| **2** | **Decompose** | Break question into sub-questions, hypotheses (`H1_LEADING`, `H2_ALTERNATIVE`, `H0_NULL`), variables, and falsification tests | `QuestionTreeAndHypothesisMap` |
| **3** | **Plan** | Select methods, source hierarchy, search strategy, datasets, and validation rules | `ResearchPlan` |
| **4** | **Acquire** | Gather primary documents, structured datasets, literature, filings, and field signals | `SourceRegister` (`List[SourceRecord]`) |
| **5** | **Validate** | Verify authenticity, provenance, timestamps, independence groups, and data hygiene | `EvidenceLedger` (`verification_summary`, `claims`, `evidence_links`) |
| **6** | **Analyze** | Run quantitative, qualitative, causal, comparative, and scenario work | `ReproducibleWorkpapers` + `DataLineageRecord` |
| **7** | **Challenge** | Run red-team, disconfirming search, sensitivity, and fragility checks | `RedTeamMemo` |
| **8** | **Review** | Audit claims, citations, math, logic, compliance, and tone | `ReviewRecord` (10 Audits + 17 Failure Modes) |
| **9** | **Communicate** | Produce structured dossier, executive brief, tables, charts, and machine schema | `14-Section Deep Dossier` + `12-Section Investment Dossier` + `ResearchPackage` |
| **10** | **Monitor** | Define signposts, triggers, watchlists, and scheduled re-evaluations | `UpdatePlanAndChangeLog` + `KnowledgeGraphNodes` |
| **11** | **Learn** | Score accuracy, calibration, latency, and post-mortem lessons | `ResearchPerformanceRecord` (13 Metrics) |

---

## Section 5 & Section 6. Evidence Architecture, 5-Tier Source Hierarchy, Independence Grouping & Data Lineage

### 5.1 Five-Tier Source Hierarchy (`SOURCE_TIER_HIERARCHY`)
- **Tier A — Primary (`credibility_floor: 0.90`)**: Regulatory filings (`10-K`, `10-Q`, `8-K`, `20-F`), official statistical agencies (`BLS`, `BEA`, `Eurostat`, `IMF`, `BIS`, `Federal Reserve`, `ECB`, `BoJ`), statutes, court filings, raw exchange data.
- **Tier B — Authoritative Secondary (`credibility_floor: 0.80`)**: Peer-reviewed journals (`Semantic Scholar`, `PubMed`, `Crossref`), NBER/CEPR/central-bank working papers, major rating agencies.
- **Tier C — Informed Secondary (`credibility_floor: 0.65`)**: Reputable financial/technical press (`Reuters`, `Bloomberg`, `FT`, `WSJ`), `arXiv` preprints, grounded `Wikipedia` summaries, verified `Chronicle` memories.
- **Tier D — Market, Sentiment & Crowd Signals (`credibility_floor: 0.45`)**: `GDELT` news tone, `HackerNews`, social/forum sentiment (`Pulse`/`Sentinel` raw feeds). Permitted to measure narrative/sentiment only; never standalone factual proof.
- **Tier E — Synthetic, Anonymous, or Unverified (`credibility_floor: 0.15`)**: Unattributed pages, rumors, screenshots without provenance. **Quarantined** by `PrimarySourceVerifierSpecialist` and barred from supporting factual claims.

### 5.2 Mandatory 18-Field Source Register (`SourceRecord`)
Every source is logged with: `source_id`, `title`, `publisher`, `author_or_institution`, `source_tier`, `source_type`, `url_or_locator`, `published_at`, `event_effective_at`, `retrieved_at`, `jurisdiction_or_universe`, `access_method`, `license_or_usage_constraints`, `content_hash`, `verbatim_extract_or_table_ref`, `extraction_confidence`, `bias_incentive_conflict_notes`, and `independence_group`.

### 5.3 Syndication & Source-Laundering Deduplication (`compute_independence_group`)
Five articles repeating the same press release or wire story are assigned the same `independence_group` (`syndicated_origin:...` or `content_stem:...`) and collapsed into **1 independent origin**, preventing echo-chamber inflation of confidence.

### 5.4 Temporal Point-in-Time Discipline & Data Lineage (`DataLineageRecord`)
Every source separates `published_at`, `event_effective_at`, and `retrieved_at`. Any source with `published_at > publication_cutoff_timestamp` is flagged in `look_ahead_violations_detected` and excluded from admissible evidence.

---

## Section 7, Section 8 & Section 9. Multi-Domain Analytics, Causal DAGs & Scenario Forecasting

- **Section 7 Domain Playbooks (`MultiDomainAnalyticalEngine`)**:
  - `fundamental_and_business`: Revenue/margin drivers, GAAP vs non-GAAP bridge, CFO-to-EBITDA conversion, debt maturity stress, WACC/terminal-growth sensitivity.
  - `quantitative_and_statistical`: Sample size, mean/std/min/max, t-statistic, two-tailed p-value, Holm-Bonferroni / Deflated Sharpe multiple-testing adjustment, volatility regime conditioning.
  - `macro_policy_and_geopolitical`: Structural vs cyclical decomposition, Taylor-rule central bank reaction function, policy transmission channels, second-order spillovers.
  - `market_and_trading`: Microstructure/session liquidity quality, spread/slippage modeling, rate differentials, positioning crowding.
  - `technology_science_and_qualitative`: Benchmark realism, train/test leakage checks, replication status across Tier A/B literature.
  - `forensic_verification`: Claim-by-claim triangulation, syndication collapse audit, look-ahead and Tier E quarantine logs.
- **Section 8 Causal Reasoning (`CausalGraphAnalysis`)**: Explicitly maps `primary_driver_x -> mediating_mechanism_m -> outcome_y`, confounders `Z`, colliders to avoid, reverse-causality checks, 4 counterfactual prompts, and falsification triggers.
- **Section 9 Forecasting & Scenario Intelligence (`ScenarioForecastSuite`)**: Anchors on outside-view base rate (`0.50`), applies evidence-weighted inside-view adjustment, reports a 90% confidence interval, and constructs 4 explicit scenarios (`base`, `upside`, `downside`, `stress`) whose probabilities sum to `1.0000`, tracked with Brier calibration uncertainty.

---

## Section 10, Section 11 & Section 12. Writing Standards, 9-Dimension Confidence, Risk Register & 10-Audit QA

- **Section 10 & 17 Dossier Formats**:
  - **14-Section Deep-Research Dossier (`deep_research_dossier_14_sections`)**: Title & Document Control, Executive Conclusion, Mandate/Scope/Definitions, Key Findings Table, Background & Structural Context, Empirical & Fundamental Analysis, Causal Mechanisms, Competing Hypotheses & Red-Team Challenge, Scenarios/Forecasts/Signposts, Risks/Limitations/Data Gaps, Decision & Research Implications, Monitoring Plan, Source Register, Technical & Reproducibility Appendix.
  - **12-Section Investment Research Dossier (`investment_research_dossier_12_sections`)**: Standard institutional buy-side/sell-side structure.
- **Section 11 Nine-Dimension Confidence Matrix (`NineDimensionConfidenceMatrix`)**:
  - Scores `source_quality`, `independence`, `completeness`, `method_fit`, `data_integrity`, `reproducibility`, `robustness`, `counterevidence`, and `freshness`, plus separate confidence scores for `facts`, `interpretations`, `causal_claims`, and `forecasts`.
- **Section 12 Ten Pre-Publication Audits & Section 16 Seventeen Failure Modes (`GovernanceAuditSpecialist`)**:
  - Runs all 10 audits (`1_claim_audit` through `10_language_audit`) and enforces automated controls across all 17 catalogued failure modes (`FM-01` through `FM-17`).

---

## Section 13, Section 14 & Section 15. Knowledge Hygiene, 13-Specialist Architecture & 13 Performance Metrics

- **Section 13 Institutional Memory Hygiene**: Only verified/corroborated claims with source links are promoted to durable semantic memory; unverified leads and self-generated summaries are quarantined from becoming circular primary facts.
- **Section 14 13-Component Specialist Architecture**: `ResearchOrchestrator`, `DomainRouter`, `SourceDiscovery`, `PrimarySourceVerifier`, `DataAnalyst`, `QuantitativeAnalyst`, `QualitativeAnalyst`, `CausalScenarioAnalyst`, `SkepticRedTeam`, `ResearchEditor`, `MemoryInterface`, `MonitoringAgent`, `GovernanceAudit`.
- **Section 15 13-Metric Performance Record**: Tracks `claim_support_rate`, `primary_source_coverage`, `independent_corroboration`, `citation_precision`, `contradiction_detection`, `factual_error_rate`, `reproducibility`, `forecast_calibration_error`, `research_completeness`, `time_to_defensible_answer_sec`, `update_latency_sec`, `reviewer_disagreement`, and `decision_usefulness`.

---

## Section 18 & Section 19. Machine-Readable Schemas & Universe Agent Handoffs

- **Section 18 Schemas**: `ResearchPackage` (`atlas.v2.research_package.v1`), `ClaimRecord`, `SourceRecord`, `EvidenceLink`.
- **Section 19 Universe Agent Handoffs (`UniverseAgentHandoffBuilder`)**:
  - `Oracle`: Macro, fundamental, event, regime, and cross-asset research context + invalidation triggers (non-execution).
  - `Chronicle`: Verified claims promoted vs quarantined claims held back + full Source Register & Evidence Links.
  - `Forge`: Empirical microstructure workpapers, causal mechanism, quantitative workpaper, counter-thesis, and falsification triggers for strategy training.
  - `Sentinel_Pulse`: Primary-source verification status and syndication deduplication for breaking alerts.
  - `Genesis`: Hypothesis map, red-team critique, and known gaps for novel hypothesis generation.
  - `Aegis`: Risk register, tail-stress scenario, and 10-audit compliance record.
  - `Universal_Coordinator`: Executive thesis, confidence matrix, and escalation status.

---

## Section 20–25. Frozen V1 Preservation, Roadmap & 5-Assignment Pilot Benchmark

`AtlasV2InstitutionalEngine.run_pilot_programme_benchmark()` executes all 5 controlled pilot assignments from **Section 22** of the Blueprint:
1. **Company / Asset Deep Dive** (10-K primary filing + peer-reviewed ROIC persistence panel study)
2. **Event & Narrative Investigation** (5 syndicated copies of 1 wire release collapsed into 1 origin vs official Eurostat release)
3. **Macro-to-Market Transmission Study** (BIS & Federal Reserve working papers on 2Y rate differentials and FX carry)
4. **Quantitative Probability Forecast** (4-scenario probability sum == 1.0000 + Brier score + t-test workpaper)
5. **Adversarial Red-Team Injection Test** (deliberately injects an unattributed Tier E rumor, 3 syndicated PR copies, stale 2015 data, post-cutoff 2027 look-ahead paper, and overconfident language `"undeniably guaranteed"`, verifying that every single anomaly is caught, quarantined, deduplicated, or calibrated).

---

## Section 26. How Forge Trains Atlas (`Forge/core/atlas_research_trainer.py`)

To ensure Atlas continuously improves in what it does—just as `Oracle` improves its trading strategies through `Forge`—`ForgeAtlasResearchTrainer` trains, ablates, walk-forward validates, shadow-monitors, and certifies **5 Specialized Atlas Research Desks**:

1. `MACRO_POLICY_DESK` (`domain="macro"`, `operating_mode="thematic_research"`)
2. `QUANT_MARKET_DESK` (`domain="trading"`, `operating_mode="deep_research"`)
3. `FUNDAMENTAL_ASSET_DESK` (`domain="company"`, `operating_mode="company_asset_research"`)
4. `FORENSIC_RED_TEAM_DESK` (`domain="forensic"`, `operating_mode="forensic_research"`)
5. `SCIENTIFIC_TECH_DESK` (`domain="science"`, `operating_mode="technical_scientific_research"`)

### Key Mechanisms of Forge-for-Atlas Training:
- **60% Development / 40% Untouched Out-of-Sample Lockbox + 4-Window Walk-Forward + 500-Iteration Paired Bootstrap**: Evaluates each candidate `AtlasResearchGenome` on point-in-time historical research & forecasting episodes against **Frozen Atlas V1**, requiring positive out-of-sample Research Edge uplift, improved Brier score calibration, and $\ge 3/4$ positive walk-forward windows.
- **Leave-One-Out Ablation Across 8 Research Evidence & Verification Signals**: Tests `primary_tier_a_anchor`, `peer_reviewed_tier_b_synthesis`, `syndication_origin_deduplicator`, `point_in_time_cutoff_gate`, `causal_dag_confounder_filter`, `outside_view_base_rate_anchor`, `skeptic_red_team_haircut`, and `crowd_sentiment_tier_d_lead`, pruning noisy signals that degrade out-of-sample Brier accuracy.
- **Dynamic Multi-Round Investigation Re-Analysis (`evaluate_live_investigation_reanalysis`)**: Mirrors real-trader live position re-analysis in the research domain:
  - **Early Sufficiency Lock**: Stops after Round 1 when primary corroboration and confidence exceed `learned_sufficiency_stop_conf`.
  - **Early False-Narrative Cut**: Cuts syndicated wire narratives immediately when contradicted by primary Tier A releases.
  - **Round-2 Primary Archive Escalation**: Automatically escalates to deeper Tier A/B retrieval when Round 1 detects syndication echo chambers or mid-band uncertainty.
- **Atlas Shadow Research Ledger & Research Mistake Clinic**: Logs predictions and resolved outcomes to `Atlas/memory/shadow_research_ledger.json` and diagnoses miscalibrated calls (`SYNDICATION_ECHO_TRAP`, `CROWD_NOISE_CONTAMINATION`, `OVERCONFIDENT_FORECAST_BRIER_MISS`, `PRIMARY_ANCHOR_UNDERWEIGHTED`) into `Atlas/memory/research_mistake_clinic.jsonl`, which feeds directly into the next Forge training cycle.

