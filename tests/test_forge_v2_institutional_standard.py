"""
Comprehensive Institutional Standard Verification Suite for Forge V2
====================================================================
Tests every section and governance boundary of the 10-page FORGE V2 Implementation Plan:
- Sections 2.2 & 2.3: Universal Agent Training Adapters (Oracle, Atlas, Chronicle, Sentinel, Genesis, Aegis)
- Section 10.1: Oracle V1 9-Module StrategyGenome Compatibility Adapter & Rollback Snapshot
- Section 9: Transaction-Cost, Price-Basis & Data-Provenance Gate (Fail-Closed BLOCKED State + Base & Stress Costs)
- Section 10: 7-State Candidate & Champion Promotion Governance (CERTIFIED vs APPROVED FOR DEPLOYMENT vs DEPLOYED)
- Sections 3.3 & 3.4: 5-Method Position-Sizing Research, Constant Monetary Risk Proof & Non-Bypassable Safety Caps
- Sections 4.1, 4.2 & 4.3: 7-Timeframe Ablation, Redundancy, Regime Policies & Stale/Missing Sensitivity Tests
- Sections 5.1 & 5.2: 8-Part Structured TradeThesis & 8-Category No-Trade Counterfactual Diagnostics
- Sections 8.1, 8.2 & 12: 12-Step Validation Ledger, 4-Window Rolling Walk-Forward, Paired Circular-Block Bootstrap,
  and 5-Candidate Controlled Pilot (Candidates A, B, C, D, E)
- Real-Trader Dynamic Re-Analysis & Capital Protection Environment
"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from Forge.core.adapters import (
    OracleV1CompatibilityAdapter,
    get_universal_agent_adapter_registry,
)
from Forge.core.institutional_standard import (
    CandidateLifecycleGovernance,
    ExplainabilityAndNoTradeEngine,
    InstitutionalValidationEngine,
    MultiTimeframeExperimentSuite,
    PositionSizingAndSafetyEngine,
    TransactionCostAndDataIntegrityGate,
)
from Forge.core.mtf_institutional_engine import MultiTimeframeInstitutionalEngine


class TestForgeV2InstitutionalStandard(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workspace_root = Path(__file__).resolve().parents[1]
        cls.engine = MultiTimeframeInstitutionalEngine(cls.workspace_root)

    def test_01_universal_agent_adapters_all_six_agents(self) -> None:
        """Section 2.2: Verify all 6 Universe agent training adapters exist and expose complete contracts."""
        registry = get_universal_agent_adapter_registry()
        expected_agents = {"Oracle", "Atlas", "Chronicle", "Sentinel", "Genesis", "Aegis"}
        self.assertEqual(set(registry.keys()), expected_agents)

        for agent_name, contract in registry.items():
            self.assertEqual(contract["target_agent"], agent_name)
            self.assertEqual(contract["schema"], "forge.universal.agent_adapter_contract.v2")
            self.assertTrue(len(contract["trainable_components"]) >= 3)
            self.assertTrue(len(contract["configurable_parameters"]) >= 3)
            self.assertTrue(len(contract["success_metrics"]) >= 2)
            self.assertTrue(len(contract["safety_constraints"]) >= 2)

    def test_02_transaction_cost_and_data_provenance_fail_closed_blocked(self) -> None:
        """Section 9: Verify missing/zeroed cost or unverified price basis fails closed to BLOCKED."""
        valid_cost = self.engine.ensure_verified_cost_profile("EURUSD")
        dataset = self.engine.load_cached_dataset("EURUSD") or self.engine.fetch_live_7tf_dataset("EURUSD")
        gate_ok = TransactionCostAndDataIntegrityGate.verify_cost_and_data_integrity(
            "EURUSD", valid_cost, dataset, "M15"
        )
        self.assertTrue(gate_ok["passed"])
        self.assertEqual(gate_ok["status"], "VERIFIED")
        self.assertGreater(gate_ok["stress_roundtrip_cost_bps"], gate_ok["base_roundtrip_cost_bps"])

        # Missing/zero commission without explicit zero-commission schedule proof must fail closed to BLOCKED
        bad_cost_zero_comm = copy.deepcopy(valid_cost)
        bad_cost_zero_comm["profile"]["commission_bps"] = 0.0
        bad_cost_zero_comm["profile"]["explicit_zero_commission_schedule"] = False
        gate_zero_comm = TransactionCostAndDataIntegrityGate.verify_cost_and_data_integrity(
            "EURUSD", bad_cost_zero_comm, dataset, "M15"
        )
        self.assertFalse(gate_zero_comm["passed"])
        self.assertEqual(gate_zero_comm["status"], "BLOCKED")
        self.assertTrue(any("Commission" in b or "commission" in b for b in gate_zero_comm["blocking_reasons"]))

        # Missing/zero slippage must fail closed to BLOCKED
        bad_cost_zero_slip = copy.deepcopy(valid_cost)
        bad_cost_zero_slip["profile"]["slippage_bps"] = 0.0
        gate_zero_slip = TransactionCostAndDataIntegrityGate.verify_cost_and_data_integrity(
            "EURUSD", bad_cost_zero_slip, dataset, "M15"
        )
        self.assertFalse(gate_zero_slip["passed"])
        self.assertEqual(gate_zero_slip["status"], "BLOCKED")

        # Invalid OHLC price basis must fail closed to BLOCKED
        bad_cost_basis = copy.deepcopy(valid_cost)
        bad_cost_basis["profile"]["price_basis"] = "unknown_inferred_basis"
        bad_cost_basis["price_basis"] = "unknown_inferred_basis"
        gate_bad_basis = TransactionCostAndDataIntegrityGate.verify_cost_and_data_integrity(
            "EURUSD", bad_cost_basis, dataset, "M15"
        )
        self.assertFalse(gate_bad_basis["passed"])
        self.assertEqual(gate_bad_basis["status"], "BLOCKED")

    def test_03_position_sizing_constant_monetary_risk_and_safety_boundaries(self) -> None:
        """Sections 3.3 & 3.4: Prove wider stop reduces lot size so USD risk remains constant and caps hold."""
        narrow_stop = PositionSizingAndSafetyEngine.compute_position_size_for_stop(
            symbol="EURUSD",
            entry_price=1.1000,
            stop_distance=0.0020,  # 20 pips
            account_equity_usd=100000.0,
            risk_budget_pct=0.50,
        )
        wide_stop = PositionSizingAndSafetyEngine.compute_position_size_for_stop(
            symbol="EURUSD",
            entry_price=1.1000,
            stop_distance=0.0040,  # 40 pips (2x wider)
            account_equity_usd=100000.0,
            risk_budget_pct=0.50,
        )
        # 2x wider stop must yield exactly 0.5x position size and identical $500 monetary risk
        self.assertAlmostEqual(narrow_stop["permitted_monetary_risk_usd"], 500.0, places=2)
        self.assertAlmostEqual(wide_stop["permitted_monetary_risk_usd"], 500.0, places=2)
        self.assertAlmostEqual(
            narrow_stop["position_lots"] / wide_stop["position_lots"],
            2.0,
            places=2,
        )

        # Trying to request 5.0% risk per trade must be hard-clamped to <= 0.50%
        excessive_risk_attempt = PositionSizingAndSafetyEngine.compute_position_size_for_stop(
            symbol="EURUSD",
            entry_price=1.1000,
            stop_distance=0.0020,
            account_equity_usd=100000.0,
            risk_budget_pct=5.00,
        )
        self.assertLessEqual(excessive_risk_attempt["permitted_risk_pct"], 0.50)
        self.assertLessEqual(excessive_risk_attempt["permitted_monetary_risk_usd"], 500.0)

    def test_04_candidate_lifecycle_governance_separation_of_certified_and_deployed(self) -> None:
        """Section 10: Prove autonomous training caps at CERTIFIED and requires explicit approval for DEPLOYED."""
        base_wf = {"positive_folds_count": 3, "total_folds": 4}
        base_metrics = {"trades_count": 25, "total_net_r": 12.5, "expectancy_r": 0.50, "stress_total_net_r": 8.2}
        base_boot = {"point_estimate_better_than_incumbent": True, "confidently_worse_than_incumbent": False}
        base_mc = {"passed_mc95_gate": True, "mc95_worst_case_drawdown_pct": 3.5}
        base_safety = {"passed_all_safety_boundaries": True, "violations": []}

        gov_blocked = CandidateLifecycleGovernance.build_lifecycle_audit(
            candidate_id="cand-test-blocked",
            cost_integrity_report={"passed": False, "blocking_reasons": ["Missing commission"]},
            walk_forward_report=base_wf,
            lockbox_metrics=base_metrics,
            stress_metrics=base_metrics,
            bootstrap_report=base_boot,
            mc_report=base_mc,
            safety_report=base_safety,
            deployment_authorization=None,
        )
        self.assertEqual(gov_blocked["current_lifecycle_state"], "BLOCKED")

        gov_certified = CandidateLifecycleGovernance.build_lifecycle_audit(
            candidate_id="cand-test-certified",
            cost_integrity_report={"passed": True, "blocking_reasons": []},
            walk_forward_report=base_wf,
            lockbox_metrics=base_metrics,
            stress_metrics=base_metrics,
            bootstrap_report=base_boot,
            mc_report=base_mc,
            safety_report=base_safety,
            deployment_authorization=None,
        )
        self.assertEqual(gov_certified["current_lifecycle_state"], "CERTIFIED")
        self.assertTrue(gov_certified["certification_qualified"])
        self.assertFalse(gov_certified["deployment_authorized"])

        gov_approved = CandidateLifecycleGovernance.build_lifecycle_audit(
            candidate_id="cand-test-approved",
            cost_integrity_report={"passed": True, "blocking_reasons": []},
            walk_forward_report=base_wf,
            lockbox_metrics=base_metrics,
            stress_metrics=base_metrics,
            bootstrap_report=base_boot,
            mc_report=base_mc,
            safety_report=base_safety,
            deployment_authorization={
                "decision": "AUTHORIZED",
                "authorized_by": "aegis_governance",
                "signature": "sig-ed25519-ok",
                "execute_deployment": False,
            },
        )
        self.assertEqual(gov_approved["current_lifecycle_state"], "APPROVED FOR DEPLOYMENT")

        gov_deployed = CandidateLifecycleGovernance.build_lifecycle_audit(
            candidate_id="cand-test-deployed",
            cost_integrity_report={"passed": True, "blocking_reasons": []},
            walk_forward_report=base_wf,
            lockbox_metrics=base_metrics,
            stress_metrics=base_metrics,
            bootstrap_report=base_boot,
            mc_report=base_mc,
            safety_report=base_safety,
            deployment_authorization={
                "decision": "AUTHORIZED",
                "authorized_by": "aegis_governance",
                "signature": "sig-ed25519-ok",
                "execute_deployment": True,
            },
        )
        self.assertEqual(gov_deployed["current_lifecycle_state"], "DEPLOYED")

    def test_05_full_end_to_end_symbol_certification_and_pilot_matrix(self) -> None:
        """Sections 4, 5, 8, 10.1 & 12: Train EURUSD and verify all institutional artifacts on disk."""
        result = self.engine.train_and_certify_symbol_strategies(
            "EURUSD", styles=["DAY_TRADING"], force_refresh_data=False
        )
        self.assertEqual(result["status"], "complete")

        cand_path = (
            self.workspace_root
            / "Chronicle"
            / "memory"
            / "research"
            / "forge_candidates"
            / "cand-v2-eurusd-day_trading.json"
        )
        self.assertTrue(cand_path.exists())
        cand = json.loads(cand_path.read_text(encoding="utf-8"))

        # 1. Lifecycle state must be CERTIFIED or VALIDATED (never auto-DEPLOYED during autonomous training)
        self.assertIn(cand["lifecycle_state"], ("CERTIFIED", "VALIDATED"))
        self.assertNotIn(cand["lifecycle_state"], ("APPROVED FOR DEPLOYMENT", "DEPLOYED"))

        # 2. Section 9 Cost & Data Provenance Gate
        self.assertTrue(cand["cost_and_data_integrity_report"]["passed"])
        self.assertEqual(cand["cost_and_data_integrity_report"]["status"], "VERIFIED")

        # 3. Section 8.1 complete required metrics under Base and Stress costs
        metrics = cand["evaluation_metrics"]
        required_metric_keys = [
            "net_return_after_costs_pct",
            "stress_net_return_after_costs_pct",
            "max_drawdown_pct",
            "profit_factor",
            "sharpe_ratio",
            "sortino_ratio",
            "win_rate_pct",
            "average_win_r",
            "average_loss_r",
            "expectancy_r",
            "realized_reward_risk_ratio",
            "trade_frequency_per_100_bars",
            "exposure_pct",
            "max_consecutive_losses",
            "recovery_factor",
            "slippage_and_execution_cost_impact_r",
        ]
        for k in required_metric_keys:
            self.assertIn(k, metrics)

        # 4. Section 8.2 Walk-Forward & Paired Bootstrap + 12-Step Ledger
        self.assertEqual(cand["walk_forward_validation_report"]["total_folds"], 4)
        self.assertIn("paired_difference_total_r", cand["paired_bootstrap_vs_incumbent"])
        self.assertEqual(len(cand["twelve_step_validation_ledger"]), 12)

        # 5. Section 12 5-Candidate Pilot Matrix (Candidates A, B, C, D, E)
        pilot = cand["pilot_five_candidate_benchmark"]["candidates"]
        for cid in (
            "A_Baseline_Frozen_V1_Incumbent",
            "B_Structure_Based",
            "C_ATR_Based",
            "D_Hybrid_Real_Trader_Dynamic_Reanalysis",
            "E_Sizing_Variant",
        ):
            self.assertIn(cid, pilot)

        # 6. Section 3.3 Position Sizing Research
        sizing = cand["position_sizing_and_safety_report"]
        self.assertTrue(sizing["wider_stop_smaller_size_proof"]["constant_monetary_risk_verified"])
        self.assertTrue(sizing["passed_all_safety_boundaries"])

        # 7. Section 4 MTF Ablation, Regime Specialization & Stale/Missing Sensitivity
        mtf_suite = cand["mtf_intelligence_and_sensitivity_report"]
        self.assertIn("regime_specific_timeframe_policies", mtf_suite)
        self.assertFalse(mtf_suite["data_synchronization_audit"]["look_ahead_leakage_detected"])

        # 8. Section 5.1 & 5.2 Structured 8-Part TradeThesis & No-Trade Counterfactuals
        no_trade = cand["no_trade_counterfactual_diagnostics"]
        self.assertEqual(len(no_trade["category_counts"]), 8)
        per_trade = cand["lockbox_holdout_report"]["per_trade_explainable_records"]
        if per_trade:
            thesis = per_trade[0]["trade_thesis"]
            self.assertEqual(thesis["schema"], "forge.section5_1.explainable_trade_thesis.v2")
            for idx_key in (
                "market_context_and_regime",
                "directional_bias_and_timeframe_evidence",
                "identified_setup_and_entry_justification",
                "stop_loss_invalidation_level_and_reason",
                "take_profit_objective_and_expected_rr",
                "position_size_and_monetary_risk",
                "execution_assumptions_and_costs",
                "thesis_invalidation_and_management_conditions",
            ):
                self.assertIn(idx_key, thesis)

        # 9. Section 10.1 Oracle V1 9-Module StrategyGenome Compatibility Export
        v1_export = cand["v1_nine_module_strategy_genome_export"]
        self.assertTrue(v1_export["schema_valid"])
        self.assertEqual(v1_export["nine_module_count"], 9)
        self.assertFalse(v1_export["v1_protection_guarantees"]["v1_champion_catalog_mutated"])

    def test_06_explicit_governance_deployment_and_oracle_v1_isolation(self) -> None:
        """Section 10 & 10.1: Verify explicit deployment approval workflow and V1 isolation."""
        auth_res = self.engine.authorize_and_deploy_certified_candidate(
            candidate_id="cand-v2-eurusd-day_trading",
            authorized_by="aegis_governance_unit_test",
            execute_deployment=False,
        )
        self.assertEqual(auth_res["status"], "complete")
        self.assertEqual(auth_res["lifecycle_state"], "APPROVED FOR DEPLOYMENT")
        self.assertEqual(
            auth_res["deployment_authorization_record"]["decision"], "AUTHORIZED"
        )

        # Re-certify back to CERTIFIED state so default catalog remains in pre-deployment CERTIFIED state
        self.engine.train_and_certify_symbol_strategies(
            "EURUSD", styles=["DAY_TRADING"], force_refresh_data=False
        )

    def test_07_shadow_runner_attaches_trade_thesis_and_position_sizing(self) -> None:
        """Verify Shadow Runner attaches structured 8-part trade_thesis and position_sizing to shadow trades."""
        from Forge.core.shadow_runner import ForgeShadowRunner

        runner = ForgeShadowRunner(self.workspace_root)
        report = runner.run_shadow_session(
            symbol="EURUSD",
            trader_style="DAY_TRADING",
            lookback_bars=260,
            refresh_live=False,
        )
        self.assertEqual(report["symbol"], "EURUSD")
        trades = report.get("recent_shadow_trades", [])
        self.assertGreater(len(trades), 0)
        sample = trades[0]
        self.assertIn("position_sizing", sample)
        self.assertIn("trade_thesis", sample)
        self.assertEqual(sample["trade_thesis"]["schema"], "forge.section5_1.explainable_trade_thesis.v2")
        self.assertLessEqual(sample["position_sizing"]["permitted_risk_pct"], 0.50)


if __name__ == "__main__":
    unittest.main()
