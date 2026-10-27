import json


def test_decision_observation_is_fsynced_to_the_durable_outbox(tmp_path, monkeypatch):
    from shared import forge_bridge_client

    outbox = tmp_path / "forge_bridge_outbox.jsonl"
    monkeypatch.setattr(forge_bridge_client, "_OUTBOX", outbox)
    client = forge_bridge_client.ForgeBridgeClient.__new__(forge_bridge_client.ForgeBridgeClient)
    client._lock = __import__("threading").RLock()
    client._wake = __import__("threading").Event()

    observation = {
        "schema": "oracle.decision.observation.v1",
        "event_id": "event-1",
        "symbol": "EURUSD",
        "evaluated_at_unix": 1_800_000_000.0,
        "outcome_code": "no_setup",
        "execution_environment": "configured",
        "details": {"confidence": 0.32},
    }
    result = client.act("oracle.decision.observation", {"observation": observation})

    assert result["status"] == "queued"
    envelope = json.loads(outbox.read_text(encoding="utf-8"))
    assert envelope["task"] == "oracle.decision.observation"
    assert envelope["context"]["observation"] == observation


def test_bridge_outbox_accepts_complete_decision_delivery(monkeypatch, tmp_path):
    from shared import forge_bridge_client

    outbox = tmp_path / "forge_bridge_outbox.jsonl"
    outbox.write_text(json.dumps({
        "task": "oracle.decision.observation",
        "context": {"observation": {
            "schema": "oracle.decision.observation.v1", "event_id": "event-1",
            "symbol": "EURUSD", "evaluated_at_unix": 1_800_000_000.0,
            "outcome_code": "no_setup", "execution_environment": "demo", "details": {},
        }},
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(forge_bridge_client, "_OUTBOX", outbox)
    client = forge_bridge_client.ForgeBridgeClient.__new__(forge_bridge_client.ForgeBridgeClient)
    client.token = "test-token"
    client.timeout = 1.0
    client._lock = __import__("threading").RLock()
    client._stop = __import__("threading").Event()
    monkeypatch.setattr(client, "_post", lambda *_args: {
        "status": "complete", "result": {"status": "complete"}})

    client._flush_outbox()

    assert not outbox.exists()


def test_trader_heartbeat_is_fsynced_to_the_durable_outbox(tmp_path, monkeypatch):
    from shared import forge_bridge_client

    outbox = tmp_path / "forge_bridge_outbox.jsonl"
    monkeypatch.setattr(forge_bridge_client, "_OUTBOX", outbox)
    client = forge_bridge_client.ForgeBridgeClient.__new__(forge_bridge_client.ForgeBridgeClient)
    client._lock = __import__("threading").RLock()
    client._wake = __import__("threading").Event()
    heartbeat = {
        "schema": "oracle.trader.heartbeat.v1", "event_id": "heartbeat-1",
        "instance_id": "runtime-test", "observed_at_unix": 1_800_000_000.0,
        "cycle": 1, "symbols": ["EURUSD"], "summary": {"scanned": 1},
    }

    result = client.act("oracle.trader.heartbeat", {"heartbeat": heartbeat})

    assert result["status"] == "queued"
    envelope = json.loads(outbox.read_text(encoding="utf-8"))
    assert envelope["task"] == "oracle.trader.heartbeat"
    assert envelope["context"]["heartbeat"] == heartbeat


def test_malformed_outbox_row_is_dead_lettered_and_does_not_block_later_heartbeat(
        monkeypatch, tmp_path):
    from shared import forge_bridge_client

    outbox = tmp_path / "forge_bridge_outbox.jsonl"
    dead_letter = tmp_path / "forge_bridge_dead_letter.jsonl"
    heartbeat = {
        "schema": "oracle.trader.heartbeat.v1", "event_id": "heartbeat-1",
        "instance_id": "runtime-test", "observed_at_unix": 1_800_000_000.0,
        "cycle": 1, "symbols": ["EURUSD"], "summary": {"scanned": 1},
    }
    valid_row = json.dumps({
        "task": "oracle.trader.heartbeat", "context": {"heartbeat": heartbeat},
    }, sort_keys=True, separators=(",", ":"))
    outbox.write_text("not-json\n" + valid_row + "\n", encoding="utf-8")
    monkeypatch.setattr(forge_bridge_client, "_OUTBOX", outbox)
    monkeypatch.setattr(forge_bridge_client, "_DEAD_LETTER", dead_letter)
    client = forge_bridge_client.ForgeBridgeClient.__new__(forge_bridge_client.ForgeBridgeClient)
    client.token = "test-token"
    client.timeout = 1.0
    client._lock = __import__("threading").RLock()
    client._stop = __import__("threading").Event()
    delivered = []

    def persisted(task, context):
        delivered.append((task, context))
        return {"status": "complete", "result": {"status": "complete"}}

    monkeypatch.setattr(client, "_post", persisted)
    client._flush_outbox()

    quarantined = json.loads(dead_letter.read_text(encoding="utf-8").splitlines()[0])
    assert quarantined["schema"] == "oracle.forge_bridge.dead_letter.v1"
    assert quarantined["raw_row"] == "not-json"
    assert delivered == [("oracle.trader.heartbeat", {"heartbeat": heartbeat})]
    assert not outbox.exists()


def test_permanent_bridge_validation_response_is_quarantined_but_transient_is_retained(
        monkeypatch, tmp_path):
    from shared import forge_bridge_client

    outbox = tmp_path / "forge_bridge_outbox.jsonl"
    dead_letter = tmp_path / "forge_bridge_dead_letter.jsonl"
    observation = {
        "schema": "oracle.decision.observation.v1", "event_id": "event-1",
        "symbol": "EURUSD", "evaluated_at_unix": 1_800_000_000.0,
        "outcome_code": "no_setup", "execution_environment": "demo", "details": {},
    }
    row = json.dumps({"task": "oracle.decision.observation",
                      "context": {"observation": observation}})
    outbox.write_text(row + "\n", encoding="utf-8")
    monkeypatch.setattr(forge_bridge_client, "_OUTBOX", outbox)
    monkeypatch.setattr(forge_bridge_client, "_DEAD_LETTER", dead_letter)
    client = forge_bridge_client.ForgeBridgeClient.__new__(forge_bridge_client.ForgeBridgeClient)
    client.token = "test-token"
    client.timeout = 1.0
    client._lock = __import__("threading").RLock()
    client._stop = __import__("threading").Event()
    monkeypatch.setattr(client, "_post", lambda *_args: {
        "status": "unavailable", "http_status": 422})

    client._flush_outbox()

    quarantined = json.loads(dead_letter.read_text(encoding="utf-8").splitlines()[0])
    assert quarantined["reason"] == "permanent_http_422"
    assert not outbox.exists()

    outbox.write_text(row + "\n", encoding="utf-8")
    monkeypatch.setattr(client, "_post", lambda *_args: {
        "status": "unavailable", "http_status": 503})
    client._flush_outbox()
    assert outbox.read_text(encoding="utf-8") == row + "\n"
