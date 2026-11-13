from fastapi import APIRouter
import time

router = APIRouter(prefix="/oracle", tags=["Oracle"])

@router.get("/status")
async def get_status():
    return {
        "agent": "oracle",
        "version": "2.0.0",
        "status": "active",
        "role": "Wall Street CIO & Institutional Portfolio/Risk/Opportunity Manager",
        "sub_agents": {
            "scout": "Cross-sectional opportunity tournament (Forge blueprint + Sentinel news + Pulse social + Chronicle memory + HTF structure + spread efficiency)",
            "portfolio": "Self-learning capital allocation, empirical Kelly sizing, learned session slot & daily trade budgets, asset-class budgets",
            "risk": "Empirical return correlation, shared currency-leg exposure, learned news/social shock gates, adaptive post-trade cooldowns"
        },
        "metrics": {
            "workflow": [
                "cross_sectional_opportunity_scouting",
                "sentinel_and_pulse_intelligence_fusion",
                "empirical_correlation_and_risk_gating",
                "adaptive_kelly_and_slot_allocation",
                "decision_and_counterfactual_ledger",
                "atlas_and_forge_deficiency_feedback"
            ],
            "champion_scope": "symbol_and_market_regime",
            "fitness_inputs": [
                "net_profit",
                "max_drawdown",
                "sharpe",
                "sortino",
                "profit_factor",
                "recovery_factor",
                "consistency",
                "out_of_sample_performance"
            ],
            "collaborators": ["atlas", "forge", "chronicle", "sentinel", "pulse", "aegis", "nexus"]
        },
        "timestamp": time.time()
    }

@router.get("/constitution")
async def get_constitution():
    return {
        "agent": "oracle",
        "version": "2.0.0",
        "not": ["random optimizer", "hardcoded rulebook", "in-process strategy trainer"],
        "is": "Wall Street CIO & scientific validator of trading capital allocation",
        "constitutional_laws": [
            "zero_hardcoded_variables",
            "continuous_learning_from_wins_losses_and_counterfactuals",
            "sentinel_and_pulse_first_class_integration",
            "every_failure_explicitly_exposed",
            "strategy_training_delegated_to_forge"
        ],
        "responsibilities": [
            "scout and rank all candidate setups across symbols",
            "manage portfolio capital, session slots, and daily trade budgets adaptively",
            "enforce empirical return and currency correlation risk gates",
            "record every entered, closed, and unentered trade for counterfactual replay",
            "query Atlas for frontier trading research and direct Forge on strategy deficiencies"
        ]
    }
