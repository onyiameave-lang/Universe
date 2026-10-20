"""Request one non-trading Oracle/Forge review from a running local API."""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def _redact(value):
    if isinstance(value, dict):
        return {
            key: ("[REDACTED]" if re.search(r"secret|token|password|credential", str(key), re.I)
                  else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        for key in ("CTRADER_CLIENT_SECRET", "CTRADER_ACCESS_TOKEN"):
            secret = os.getenv(key)
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    return value


def main() -> int:
    symbol = os.getenv("ORACLE_FORGE_SYMBOL", "EURUSD").strip().upper()
    if not re.fullmatch(r"[A-Z0-9._-]{3,24}", symbol):
        raise SystemExit("ORACLE_FORGE_SYMBOL has an invalid format")
    experiment_type = os.getenv("ORACLE_FORGE_EXPERIMENT_TYPE", "challenger").strip().lower()
    if experiment_type not in {"challenger", "ablation"}:
        raise SystemExit("ORACLE_FORGE_EXPERIMENT_TYPE must be challenger or ablation")
    ablation_target = os.getenv("ORACLE_FORGE_ABLATION_TARGET", "").strip()
    targets = []
    if experiment_type == "ablation":
        allowed = {"trend", "momentum", "volatility", "entry", "exit", "risk", "position", "trade_management", "market_regime"}
        if ablation_target not in allowed:
            raise SystemExit("Ablation run requires one registered ORACLE_FORGE_ABLATION_TARGET")
        targets = [ablation_target]
    url = os.getenv("ORACLE_FORGE_API_URL", "http://127.0.0.1:8000/oracle/forge/experiment")
    payload = json.dumps({
        "symbol": symbol,
        "reason": "scheduled Forge challenger review" if experiment_type == "challenger" else f"Forge component ablation: {ablation_target}",
        "ablation_targets": targets,
    }).encode()
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    response_data = None
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        response_data = {"status": "error", "error": f"Forge request failed: {type(exc).__name__}"}

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    reports = Path("Forge/reports/oracle_experiments")
    reports.mkdir(parents=True, exist_ok=True)
    report_path = reports / f"{stamp}-{symbol}.json"
    report_path.write_text(json.dumps({
        "schema": "forge.oracle.action_report.v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "experiment_type": experiment_type,
        "ablation_targets": targets,
        "workflow": "oracle-forge-experiment",
        "result": _redact(response_data),
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result = response_data if isinstance(response_data, dict) else {}
    inner = result.get("data") if isinstance(result.get("data"), dict) else {}
    print(json.dumps({
        "report": str(report_path),
        "status": result.get("status", inner.get("status", "unknown")),
        "experiment_id": inner.get("experiment_id"),
    }))
    return 0 if response_data is not None else 1


if __name__ == "__main__":
    sys.exit(main())
