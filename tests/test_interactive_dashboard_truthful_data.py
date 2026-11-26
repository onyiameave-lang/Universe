import json
import time

from Nexus.api import interactive_actions as actions


def test_dashboard_keeps_stale_positions_labeled_as_last_known(tmp_path, monkeypatch):
    snapshot_dir = tmp_path / "Oracle" / "memory"
    snapshot_dir.mkdir(parents=True)
    snapshot = {
        "source": "authenticated_ctrader_open_api",
        "account_type": "demo",
        "account_info_available": True,
        "connected": True,
        "balance": 4000.0,
        "equity": 4012.0,
        "currency": "USD",
        "account_updated_at_unix": time.time() - 600,
        "positions_updated_at_unix": time.time() - 600,
        "positions": [{"ticket": 11, "symbol": "EURUSD", "direction": "buy", "volume": 1000,
                       "entry_price": 1.08, "current_price": None, "floating_pnl": None}],
        "history_status": "complete",
        "history_updated_at_unix": time.time(),
        "closed_trades": [],
    }
    (snapshot_dir / "ctrader_dashboard_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(actions, "ORACLE_MEMORY_DIR", snapshot_dir)

    result = actions.get_oracle_dashboard_data("ctrader")

    assert result["positions_status"] == "unavailable_or_stale"
    assert result["live_open_trades"][0]["status"] == "LAST_KNOWN"
    assert result["live_open_trades"][0]["current_price"] is None
    assert result["live_open_trades"][0]["floating_pnl_usd"] is None
    assert result["brokers"]["ctrader"]["balance"] is None


def test_mixed_broker_snapshots_do_not_count_stale_positions_as_open(tmp_path, monkeypatch):
    now = time.time()
    snapshot_dir = tmp_path / "Oracle" / "memory"
    snapshot_dir.mkdir(parents=True)
    fresh = {
        "source": "authenticated_mt5_terminal", "account_type": "demo", "connected": True,
        "account_info_available": True, "account_updated_at_unix": now,
        "positions_updated_at_unix": now, "balance": 4000, "equity": 4010, "currency": "USD",
        "positions": [{"ticket": 1, "symbol": "EURUSD", "direction": "buy", "volume": 0.1}],
        "history_status": "unavailable_or_stale",
    }
    stale = {
        "source": "authenticated_ctrader_open_api", "account_type": "demo", "connected": True,
        "account_info_available": True, "account_updated_at_unix": now - 600,
        "positions_updated_at_unix": now - 600, "balance": 5000, "equity": 4990, "currency": "USD",
        "positions": [{"ticket": 2, "symbol": "BTCUSD", "direction": "sell", "volume": 1000}],
        "history_status": "unavailable_or_stale",
    }
    (snapshot_dir / "mt5_dashboard_snapshot.json").write_text(json.dumps(fresh), encoding="utf-8")
    (snapshot_dir / "ctrader_dashboard_snapshot.json").write_text(json.dumps(stale), encoding="utf-8")
    monkeypatch.setattr(actions, "ORACLE_MEMORY_DIR", snapshot_dir)

    result = actions.get_oracle_dashboard_data("all")

    assert result["positions_status"] == "partial"
    assert result["open_trades"] == 1
    assert result["last_known_position_count"] == 1
    assert [row["status"] for row in result["live_open_trades"]] == ["OPEN", "LAST_KNOWN"]


def test_chart_does_not_substitute_missing_timeframe_or_quote(tmp_path, monkeypatch):
    symbol_dir = tmp_path / "EURUSD"
    symbol_dir.mkdir()
    dataset = {
        "source": "test_feed",
        "fetched_at_unix": time.time(),
        "series": {"M15": [{"ts": "2026-11-24T10:00:00Z", "open": 1.0, "high": 1.2,
                              "low": 0.9, "close": 1.1, "volume": 2}]},
    }
    (symbol_dir / "latest.json").write_text(json.dumps(dataset), encoding="utf-8")
    monkeypatch.setattr(actions, "MTF_DIR", tmp_path)
    monkeypatch.setattr(actions, "_MTF_CACHE_PATH", None)
    monkeypatch.setattr(actions, "_MTF_CACHE_MTIME_NS", None)
    monkeypatch.setattr(actions, "_MTF_CACHE_DATA", {})

    missing = actions.get_oracle_chart_data("EURUSD", "M45")
    assert missing["data_status"] == "unavailable"
    assert missing["bars"] == []
    assert missing["live_bid"] is None
    assert missing["live_ask"] is None


def test_chart_never_draws_last_known_position_as_live(tmp_path, monkeypatch):
    now = time.time()
    snapshot_dir = tmp_path / "Oracle" / "memory"
    snapshot_dir.mkdir(parents=True)
    snapshot = {
        "source": "authenticated_ctrader_open_api", "account_type": "demo", "connected": True,
        "account_info_available": True, "account_updated_at_unix": now - 600,
        "positions_updated_at_unix": now - 600, "balance": 5000, "equity": 4990, "currency": "USD",
        "positions": [{"ticket": 2, "symbol": "BTCUSD", "direction": "sell", "volume": 1000}],
    }
    (snapshot_dir / "ctrader_dashboard_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    symbol_dir = tmp_path / "BTCUSD"
    symbol_dir.mkdir()
    dataset = {
        "source": "test_feed", "fetched_at_unix": now,
        "series": {"M15": [{"ts": "2026-11-24T10:00:00Z", "open": 1.0, "high": 1.2,
                                "low": 0.9, "close": 1.1, "volume": 2}]},
    }
    (symbol_dir / "latest.json").write_text(json.dumps(dataset), encoding="utf-8")
    monkeypatch.setattr(actions, "ORACLE_MEMORY_DIR", snapshot_dir)
    monkeypatch.setattr(actions, "MTF_DIR", tmp_path)
    monkeypatch.setattr(actions, "_MTF_CACHE_PATH", None)
    monkeypatch.setattr(actions, "_MTF_CACHE_MTIME_NS", None)
    monkeypatch.setattr(actions, "_MTF_CACHE_DATA", {})

    result = actions.get_oracle_chart_data("BTCUSD", "M15", "ctrader")

    assert result["positions_status"] == "unavailable_or_stale"
    assert result["all_open_trades"][0]["status"] == "LAST_KNOWN"
    assert result["open_trades"] == []


def test_m45_chart_aggregates_three_complete_m15_bars(tmp_path, monkeypatch):
    symbol_dir = tmp_path / "EURUSD"
    symbol_dir.mkdir()
    dataset = {
        "source": "test_feed",
        "fetched_at_unix": time.time(),
        "series": {"M15": [
            {"ts": "2026-11-24T09:45:00Z", "unix_sec": 1795513500, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 2},
            {"ts": "2026-11-24T10:00:00Z", "unix_sec": 1795514400, "open": 1.05, "high": 1.2, "low": 1.0, "close": 1.1, "volume": 3},
            {"ts": "2026-11-24T10:15:00Z", "unix_sec": 1795515300, "open": 1.1, "high": 1.15, "low": 0.95, "close": 1.02, "volume": 4},
        ]},
    }
    (symbol_dir / "latest.json").write_text(json.dumps(dataset), encoding="utf-8")
    monkeypatch.setattr(actions, "MTF_DIR", tmp_path)
    monkeypatch.setattr(actions, "_MTF_CACHE_PATH", None)
    monkeypatch.setattr(actions, "_MTF_CACHE_MTIME_NS", None)
    monkeypatch.setattr(actions, "_MTF_CACHE_DATA", {})

    result = actions.get_oracle_chart_data("EURUSD", "M45")

    assert len(result["bars"]) == 1
    assert result["bars"][0]["open"] == 1.0
    assert result["bars"][0]["high"] == 1.2
    assert result["bars"][0]["low"] == 0.9
    assert result["bars"][0]["close"] == 1.02
    assert result["bars"][0]["volume"] == 9
    assert result["data_source"] == "test_feed:aggregated_M15_to_M45"
    assert result["rsi14"] is None


def test_telegram_feed_reads_only_recorded_delivery_rows(tmp_path, monkeypatch):
    ledger = tmp_path / "logs" / "telegram_signal_ledger.jsonl"
    ledger.parent.mkdir()
    ledger.write_text(json.dumps({"timestamp_unix": 123, "status": "sent", "message": "recorded signal"}) + "\n"
                      + "{invalid json}\n", encoding="utf-8")
    monkeypatch.setattr(actions, "ORACLE_MEMORY_DIR", tmp_path)

    result = actions.get_telegram_signal_feed(10)

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["items"][0]["message"] == "recorded signal"


def test_legacy_manual_execution_action_fails_closed():
    result = actions.execute_oracle_action({"action": "open_trade", "symbol": "EURUSD"})
    assert result["status"] == "unavailable"
    assert result["open_trades"] == []


def test_feed_refresh_reports_partial_source_failure(tmp_path, monkeypatch):
    class WorkingCollector:
        def gather(self, **_kwargs):
            return {"articles": [{"title": "Observed headline"}], "source_status": {"rss": "ok"}}

    class FailingCollector:
        def gather(self, **_kwargs):
            raise TimeoutError("collector timeout")

    class Agent:
        def __init__(self, engine):
            self.engine = engine

    monkeypatch.setattr(actions, "FEEDS_CACHE_PATH", tmp_path / "feeds.json")
    result = actions.execute_feed_refresh("all", {
        "sentinel": Agent(WorkingCollector()),
        "pulse": Agent(FailingCollector()),
    })

    assert result["status"] == "partial"
    assert result["source_results"]["sentinel"]["status"] == "refreshed"
    assert result["source_results"]["pulse"]["status"] == "error"
    assert result["news_count"] == 1
