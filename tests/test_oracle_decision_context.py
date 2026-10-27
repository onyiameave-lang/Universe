from Oracle.agents.oracle_agent import OracleAgent
from types import SimpleNamespace


def test_decision_context_keeps_diagnostic_features_and_drops_provider_payloads():
    signal_result = {
        "signal": {
            "call": "hold", "direction": 0.08, "confidence": 0.31,
            "agreement": 0.5, "entry_threshold": 0.15,
            "learned_weights": {"technical": 0.5, "news": 0.2, "secret": 99},
            "streams": {
                "technical": {"direction": 0.4, "confidence": 0.8,
                              "source": "evolved_champion", "genome": "genome-1"},
                "news": {"direction": -0.1, "confidence": 0.2,
                         "evidence_state": "available", "articles": 2,
                         "_risk_articles": [{"text": "private article"}]},
                "social": {"direction": 0.0, "confidence": 0.0,
                           "_manipulation_flags": ["private post"]},
            },
        },
        "regime": "ranging", "last": 1.08, "atr": 0.002,
        "_technicals": {
            "rsi_14": 51.0,
            "regime": {"slope_20": 0.001, "volatility": 0.004,
                       "high_volatility": False},
        },
    }

    context = OracleAgent._decision_context(signal_result)

    assert context["schema"] == "oracle.decision_context.v1"
    assert context["fused"]["entry_threshold"] == 0.15
    assert context["market"]["regime"] == "ranging"
    assert context["market"]["atr_14"] == 0.002
    assert context["streams"]["news"]["evidence_count"] == 2
    assert "private article" not in repr(context)
    assert "private post" not in repr(context)
    assert "genome" not in repr(context)
    assert "secret" not in context["fused"].get("learned_weights", {})


def test_decision_context_adds_effective_risk_and_classifier_evidence():
    context = OracleAgent._decision_context(
        {"signal": {"call": "sell", "confidence": 0.7, "streams": {}},
         "regime": "trending_down"},
        effective_confidence=0.4,
        risk={"mode": "paper", "confidence_floor": 0.5,
              "reasons": ["do not copy arbitrary reasons"]},
        news_impact="high", social_risk="none",
    )

    assert context["fused"]["effective_confidence"] == 0.4
    assert context["risk"] == {"mode": "paper", "confidence_floor": 0.5}
    assert context["news_impact"] == "high"
    assert context["social_risk"] == "none"


def test_trade_propose_attaches_decision_context_to_hold_and_risk_rejection(monkeypatch):
    import Oracle.agents.oracle_agent as oracle_module

    hold_agent = OracleAgent.__new__(OracleAgent)
    hold_agent.signal = lambda _symbol: {
        "status": "complete", "symbol": "EURUSD", "last": 1.08, "atr": 0.002,
        "regime": "ranging", "_technicals": {"rsi_14": 49},
        "signal": {"call": "hold", "direction": 0.05, "confidence": 0.3,
                   "agreement": 0.5, "entry_threshold": 0.15,
                   "streams": {"technical": {"direction": 0.2, "confidence": 0.5}}},
    }
    held = hold_agent.execute("trade.propose", {"symbol": "EURUSD"})
    assert held["decision_context"]["fused"]["call"] == "hold"

    class _Classifier:
        def assess(self, *_args):
            return SimpleNamespace(confidence_multiplier=1.0, level="none", reason="")

    monkeypatch.setattr(oracle_module, "NewsImpactClassifier", _Classifier)
    monkeypatch.setattr(oracle_module, "SocialRiskClassifier", _Classifier)
    rejected_agent = OracleAgent.__new__(OracleAgent)
    rejected_agent.signal = lambda _symbol: {
        "status": "complete", "symbol": "EURUSD", "last": 1.08, "atr": 0.002,
        "regime": "trending_up", "_technicals": {"atr_14": 0.002},
        "using_evolved_champion": True,
        "signal": {"call": "buy", "direction": 0.7, "confidence": 0.4,
                   "agreement": 0.75, "entry_threshold": 0.15,
                   "streams": {"technical": {"direction": 0.8, "confidence": 0.8,
                                               "genome": "genome-1"},
                               "news": {}, "social": {}}},
    }
    rejected_agent.risk = SimpleNamespace(evaluate=lambda *_args: {
        "approved": False, "reasons": ["confidence below floor"],
        "mode": "paper", "confidence_floor": 0.5,
    })
    rejected_agent._preserve = lambda *_args: None
    rejected_agent.trade_journal = SimpleNamespace(log_rejection=lambda *_args: None)
    rejected = rejected_agent.execute("trade.propose", {"symbol": "EURUSD"})

    assert rejected["decision_context"]["risk"]["confidence_floor"] == 0.5
    assert rejected["decision_context"]["fused"]["effective_confidence"] == 0.4
    assert rejected["strategy_id"] == "genome-1"
