"""
Comprehensive Verification Suite for ATLAS V2: Institutional-Grade Research Agent Blueprint
(Verifies all 25 Sections of the Blueprint + Preservation of Frozen Atlas V1 Baseline + Forge Integration).
"""
from __future__ import annotations

import unittest
from Atlas.core.institutional_research_v2 import (
    INSTITUTIONAL_BENCHMARK_STANDARDS,
    NON_NEGOTIABLE_BOUNDARIES,
    OPERATING_MODES,
    SEVENTEEN_FAILURE_MODES,
    SOURCE_TIER_HIERARCHY,
    AtlasV2InstitutionalEngine,
    classify_source_tier,
    compute_independence_group,
)
from Atlas.core.research_engine import ResearchEngine
from Atlas.agents.research_agent import AtlasAgent


class TestAtlasV2InstitutionalBlueprint(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = ResearchEngine()
        self.v2 = self.engine.v2_engine
        self.agent = AtlasAgent()

    def test_01_benchmarks_modes_and_boundaries(self) -> None:
        """Verify Sections 2 & 3: 7 Institutional Benchmarks, 8 Operating Modes, and 6 Non-Negotiable Boundaries."""
        self.assertEqual(len(INSTITUTIONAL_BENCHMARK_STANDARDS), 7)
        self.assertEqual(len(OPERATING_MODES), 8)
        for mode in (
            "quick_research",
            "deep_research",
            "continuous_monitoring",
            "thematic_research",
            "company_asset_research",
            "forensic_research",
            "technical_scientific_research",
            "agent_support",
        ):
            self.assertIn(mode, OPERATING_MODES)
        self.assertEqual(len(NON_NEGOTIABLE_BOUNDARIES), 6)
        self.assertEqual(len(SEVENTEEN_FAILURE_MODES), 17)

    def test_02_five_tier_source_hierarchy_and_syndication_deduplication(self) -> None:
        """Verify Section 5: 5-Tier Source Hierarchy (Tier A..E) and Syndicated Press-Release Deduplication."""
        self.assertEqual(list(SOURCE_TIER_HIERARCHY.keys()), ["Tier A", "Tier B", "Tier C", "Tier D", "Tier E"])

        tier_a, _ = classify_source_tier("sec.gov", "https://www.sec.gov/Archives/edgar/10-K.htm", "Form 10-K")
        tier_b, _ = classify_source_tier("semantic_scholar", "https://api.semanticscholar.org/paper", "Peer Reviewed Study")
        tier_c, _ = classify_source_tier("arxiv", "https://arxiv.org/abs/2501.00001", "Working Preprint")
        tier_d, _ = classify_source_tier("hackernews", "https://news.ycombinator.com/item?id=1", "HN Thread")
        tier_e, _ = classify_source_tier("anonymous_blog", "", "Unattributed rumor screenshot")

        self.assertEqual(tier_a, "Tier A")
        self.assertEqual(tier_b, "Tier B")
        self.assertEqual(tier_c, "Tier C")
        self.assertEqual(tier_d, "Tier D")
        self.assertEqual(tier_e, "Tier E")

        # 5 sites repeating the same press release must map to the exact same independence_group
        pr_text = "According to a release by GlobalAlpha Corp: Q2 operating cash flow rose 18%."
        groups = {
            compute_independence_group("web", f"Site {i} Headline", pr_text, f"https://site{i}.example.com/pr")
            for i in range(1, 6)
        }
        self.assertEqual(len(groups), 1)

    def test_03_eleven_stage_lifecycle_and_dossiers(self) -> None:
        """Verify Sections 4, 6, 7, 8, 9, 10, 11, 12, 15, 17, 18: Full 11-stage run, 14-section dossier, 12-section dossier, and ResearchPackage."""
        evidence = [
            {
                "source": "federalreserve.gov",
                "title": "Federal Reserve Monetary Policy & FX Transmission Release",
                "text": "2-year real yield differentials account for 61% of EURUSD medium-term variance.",
                "url": "https://www.federalreserve.gov/monetarypolicy/fx_2026.htm",
                "published_at": "2026-04-10T12:00:00Z",
                "credibility": 0.95,
            },
            {
                "source": "semantic_scholar",
                "title": "Empirical Regime Conditioning in G10 Currency Markets",
                "text": "Carry and rate-differential signals exhibit +1.42 Sharpe in low-volatility regimes but invert during liquidity shocks.",
                "url": "https://api.semanticscholar.org/CorpusID:998877",
                "published_at": "2026-04-18T00:00:00Z",
                "credibility": 0.90,
            },
        ]
        res = self.engine.investigate_v2(
            query="How do 2-year real yield differentials transmit to EURUSD across volatility regimes?",
            domain="trading",
            operating_mode="deep_research",
            as_of_timestamp="2026-05-01T00:00:00Z",
            publication_cutoff_timestamp="2026-05-01T00:00:00Z",
            injected_evidence=evidence,
            context_data={"series": [0.42, 0.55, 0.61, 0.58, 0.64, 0.70]},
        )
        self.assertEqual(res["status"], "complete")
        stages = res["lifecycle_stages"]
        self.assertEqual(len(stages), 11)

        # 14-Section Deep-Research Dossier (Section 17)
        deep_sections = res["deep_research_dossier_14_sections"]["sections"]
        self.assertEqual(len(deep_sections), 14)

        # 12-Section Investment Research Dossier (Section 10.2)
        inv_sections = res["investment_research_dossier_12_sections"]["sections"]
        self.assertEqual(len(inv_sections), 12)

        # Section 18 ResearchPackage
        pkg = res["research_package"]
        self.assertEqual(pkg["schema"], "atlas.v2.research_package.v1")
        self.assertGreaterEqual(len(pkg["sources"]), 2)
        self.assertGreaterEqual(len(pkg["claims"]), 2)
        self.assertGreaterEqual(len(pkg["evidence_links"]), 2)
        self.assertEqual(len(pkg["scenarios"]), 4)
        scenario_sum = sum(s["probability"] for s in pkg["scenarios"])
        self.assertAlmostEqual(scenario_sum, 1.0, places=3)

        # Section 11 9-Dimension Confidence Matrix + 4 Claim Classes
        conf = pkg["confidence_assessments"]
        self.assertEqual(len(conf["dimensions"]), 9)
        for cls_key in ("facts", "interpretations", "causal_claims", "forecasts"):
            self.assertIn(cls_key, conf["confidence_by_claim_class"])

        # Section 12 10-Audit QA & Section 15 13-Metric Performance Record
        qa = pkg["audit_trail"]["ten_audit_qa"]
        self.assertTrue(qa["all_ten_audits_passed"])
        self.assertEqual(len(qa["audits"]), 10)
        perf = pkg["audit_trail"]["performance_metrics"]
        self.assertEqual(perf["metrics_count"], 13)

    def test_04_universe_agent_handoffs_and_forge_integration(self) -> None:
        """Verify Section 19: Structured handoffs for Oracle, Chronicle, Forge, Sentinel_Pulse, Genesis, Aegis, Universal_Coordinator."""
        sample_evidence = [
            {
                "source": "bis.org",
                "title": "BIS Working Paper on FX Regime Transmission",
                "text": "Rate differentials and liquidity regimes drive medium-term FX momentum.",
                "url": "https://www.bis.org/publ/work1102.htm",
                "published_at": "2026-04-01T00:00:00Z",
                "credibility": 0.94,
            },
            {
                "source": "semantic_scholar",
                "title": "Peer-Reviewed Cross-Asset Volatility Study",
                "text": "Out-of-sample walk-forward Sharpe improves when conditioned on volatility regime.",
                "url": "https://api.semanticscholar.org/CorpusID:112233",
                "published_at": "2026-04-15T00:00:00Z",
                "credibility": 0.90,
            },
        ]
        for target in ("Oracle", "Chronicle", "Forge", "Sentinel_Pulse", "Genesis", "Aegis", "Universal_Coordinator"):
            out = self.agent.execute(
                "research.handoff",
                {
                    "target_agent": target,
                    "query": f"Institutional research handoff test for {target}",
                    "domain": "trading",
                    "injected_evidence": sample_evidence,
                },
            )
            self.assertEqual(out["status"], "complete")
            handoff = out["handoff"]
            self.assertEqual(handoff["schema"], "atlas.v2.agent_handoff.v1")
            self.assertEqual(handoff["target_agent"], target)
            self.assertIn("payload", handoff)

    def test_05_pilot_programme_and_adversarial_red_team_controls(self) -> None:
        """Verify Section 20 & Section 22: Frozen V1 baseline preserved + 5-Assignment Pilot Benchmark including Adversarial Red-Team test."""
        # Verify Frozen V1 baseline methods still exist and work
        stats = self.engine.stats()
        self.assertTrue(stats["v1_frozen_baseline"])
        self.assertTrue(stats["v2_institutional_engine"])
        h = self.engine.generate_hypothesis("Rate differentials drive medium-term FX trends", domain="trading")
        self.assertIn("hypothesis_id", h)

        # Run the 5-assignment Pilot Benchmark Suite
        bench_res = self.agent.execute("research.pilot_benchmark", {})
        self.assertEqual(bench_res["status"], "complete")
        bench = bench_res["benchmark"]
        self.assertEqual(bench["assignments_completed"], 5)
        self.assertTrue(bench["frozen_v1_baseline_preserved"])

        # Check Assignment 2: 5 syndicated wire copies + 1 Eurostat primary source -> 4 syndicated duplicates collapsed, 2 independent origins
        a2 = bench["assignments"]["2_event_and_narrative_investigation"]
        self.assertEqual(a2["raw_sources"], 6)
        self.assertEqual(a2["syndicated_duplicates_collapsed"], 4)
        self.assertEqual(a2["independent_origins_admissible"], 2)

        # Check Assignment 5: Adversarial Red-Team injection catches Tier E rumor, syndicated copies, stale 2015 data, and 2027 look-ahead bias
        a5 = bench["assignments"]["5_red_team_adversarial_injection"]
        self.assertGreaterEqual(a5["quarantined_tier_e_count"], 1)
        self.assertGreaterEqual(a5["syndicated_duplicates_collapsed"], 2)
        self.assertGreaterEqual(a5["look_ahead_violations_blocked"], 1)
        self.assertGreaterEqual(a5["stale_sources_flagged"], 1)

    def test_06_forge_trains_atlas_desks_ablation_and_shadow_mistake_clinic(self) -> None:
        """Verify Forge trains all 5 Atlas Research Desks via 60/40 Lockbox, Leave-One-Out Ablation, Walk-Forward, and Mistake Clinic."""
        from Forge.core.atlas_research_trainer import ATLAS_RESEARCH_DESKS, ForgeAtlasResearchTrainer

        trainer = ForgeAtlasResearchTrainer()

        # 1. Fail-closed check: disabling provenance/red-team verification must return BLOCKED
        blocked = trainer.train_and_certify_atlas_desk(
            desk="QUANT_MARKET_DESK",
            verify_provenance_and_red_team=False,
        )
        self.assertEqual(blocked["lifecycle_state"], "BLOCKED")

        # 2. Train all 5 Atlas Research Desks via AtlasAgent ('research.train_atlas')
        train_out = self.agent.execute("research.train_atlas", {"approve_for_deployment": False})
        self.assertEqual(train_out["status"], "complete")
        catalog = train_out["atlas_training_catalog"]
        self.assertEqual(catalog["desks_trained"], 5)

        for desk in ATLAS_RESEARCH_DESKS:
            self.assertIn(desk, catalog["desks"])
            art = catalog["desks"][desk]
            # Autonomous training caps at CERTIFIED (never auto-DEPLOYED without explicit approval)
            self.assertEqual(art["lifecycle_state"], "CERTIFIED")
            self.assertIn("ablation_study", art)
            self.assertGreaterEqual(len(art["ablation_study"]["retained_features"]), 4)
            # Verify out-of-sample lockbox uplift over Frozen Atlas V1
            uplift = art["validation_and_benchmark"]["v2_vs_frozen_v1_uplift"]
            self.assertGreater(uplift["total_research_edge_uplift_r"], 0.0)
            self.assertGreater(uplift["brier_calibration_improvement"], 0.0)

        # 3. Verify that running an Atlas V2 investigation now automatically loads the Forge-trained Desk Genome!
        res = self.engine.investigate_v2(
            query="Evaluate EURUSD rate differential transmission with Forge-trained QUANT_MARKET_DESK genome",
            domain="trading",
            operating_mode="deep_research",
            injected_evidence=[
                {
                    "source": "bis.org",
                    "title": "BIS FX Transmission Paper",
                    "text": "2Y rate differentials explain 64% of medium-term variance.",
                    "url": "https://www.bis.org/publ/work1102.htm",
                    "published_at": "2026-04-01T00:00:00Z",
                    "credibility": 0.95,
                },
                {
                    "source": "semantic_scholar",
                    "title": "Peer-Reviewed FX Carry Regime Study",
                    "text": "Regime-conditioned carry outperforms unconditioned baseline out of sample.",
                    "url": "https://api.semanticscholar.org/CorpusID:20491",
                    "published_at": "2026-04-12T00:00:00Z",
                    "credibility": 0.91,
                },
            ],
        )
        pkg = res["research_package"]
        self.assertTrue(pkg["forge_trained_desk_genome_active"])
        self.assertEqual(pkg["forge_trained_desk_name"], "QUANT_MARKET_DESK")
        self.assertTrue(trainer.shadow_ledger_path.exists())
        self.assertTrue(trainer.mistake_clinic_path.exists())


if __name__ == "__main__":
    unittest.main()
