import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import api


class _LocalRequest:
    client = SimpleNamespace(host="127.0.0.1")
    headers = {"origin": "http://127.0.0.1:5500"}

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _FakeProcess:
    pid = 43210

    def __init__(self):
        self.returncode = None
        self.sent_signals = []

    def poll(self):
        return self.returncode

    def send_signal(self, value):
        self.sent_signals.append(value)


@pytest.mark.parametrize(
    ("broker", "module", "expected_env"),
    [
        ("ctrader", "Oracle.execution.ctrader_demo_trader", "CTRADER_USE_DEMO"),
        ("mt5", "Oracle.execution.mt5_demo_trader", "ORACLE_REQUIRE_DEMO_ACCOUNT"),
    ],
)
def test_frontend_start_passes_demo_guards_and_cycle_limit(
    broker, module, expected_env, monkeypatch, tmp_path
):
    process = _FakeProcess()
    captured = {}
    monkeypatch.setattr(api, "ROOT", tmp_path)
    monkeypatch.setattr(api.subprocess, "Popen", lambda *args, **kwargs: (
        captured.update(args=args, kwargs=kwargs) or process
    ))
    api._ORACLE_RUNNERS.clear()

    result = asyncio.run(api.start_oracle_run(_LocalRequest({"broker": broker, "cycles": 4})))

    command = captured["args"][0]
    env = captured["kwargs"]["env"]
    assert result["status"] == "starting"
    assert result["cycles"] == 4
    assert command[command.index("-m") + 1] == module
    assert command[-2:] == ["--cycles", "4"]
    assert env[expected_env] == "true"
    assert env["ORACLE_ALLOW_LIVE"] == "false"
    assert captured["kwargs"]["stdin"] is api.subprocess.DEVNULL

    stopped = asyncio.run(api.stop_oracle_run(_LocalRequest({"broker": broker})))
    assert stopped["status"] == "stop_requested"
    assert len(process.sent_signals) == 1
    api._ORACLE_RUNNERS.clear()


def test_frontend_start_rejects_invalid_cycles_without_spawning(monkeypatch):
    calls = []
    monkeypatch.setattr(api.subprocess, "Popen", lambda *args, **kwargs: calls.append(args))
    api._ORACLE_RUNNERS.clear()

    with pytest.raises(api.HTTPException) as exc:
        asyncio.run(api.start_oracle_run(_LocalRequest({"broker": "ctrader", "cycles": 10001})))

    assert exc.value.status_code == 400
    assert calls == []
