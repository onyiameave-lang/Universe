from Oracle.core import risk


def test_risk_gate_blocks_reentry_after_a_win(monkeypatch):
    monkeypatch.setattr(risk, "PAPER_TRADING", True)
    monkeypatch.setattr(risk, "_WIN_COOLDOWN_SEC_PAPER", 3600)
    manager = risk.RiskManager()
    manager.portfolio.record_win("EURUSD")

    result = manager.evaluate("EURUSD", "long", 1.1, 0.01, 0.95)

    assert result["approved"] is False
    assert any("won a trade" in reason for reason in result["reasons"])


def test_win_cooldown_is_per_symbol(monkeypatch):
    monkeypatch.setattr(risk, "PAPER_TRADING", True)
    monkeypatch.setattr(risk, "_WIN_COOLDOWN_SEC_PAPER", 3600)
    manager = risk.RiskManager()
    manager.portfolio.record_win("EURUSD")

    result = manager.evaluate("GBPUSD", "long", 1.1, 0.01, 0.95)

    assert not any("won a trade" in reason for reason in result.get("reasons", []))
