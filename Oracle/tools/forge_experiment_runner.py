"""Run one bounded Oracle/Forge research mode over the configured watchlist.

Scheduled runs rotate one mode per six-hour UTC slot and process symbols
sequentially. This is research-only: it never places orders or promotes a
strategy. Completed daily-data windows are not evaluated twice.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Oracle.execution.ctrader_demo_trader import DEFAULT_SYMBOLS

ABLATIONS = (
    "trend", "momentum", "volatility", "entry", "exit", "risk", "position",
    "trade_management", "market_regime",
)
ROTATION = ("challenger", *[f"ablation:{name}" for name in ABLATIONS], "drift")
REPORT_DIR = ROOT / "Forge" / "reports" / "oracle_experiments"


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


def _cycle():
    now = datetime.now(timezone.utc)
    slot = int(now.timestamp() // (6 * 60 * 60))
    return now, slot, ROTATION[slot % len(ROTATION)]


def _previous_windows():
    seen = set()
    for path in REPORT_DIR.glob("*.json"):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for item in report.get("results", []):
            fingerprint = item.get("data_fingerprint")
            if fingerprint and item.get("status") == "complete":
                seen.add((item.get("symbol"), item.get("experiment_type"), item.get("ablation_target"), fingerprint))
    return seen


def _post(url: str, symbol: str, mode: str, ablation: str | None, symbols=None,
          execution_costs=None):
    drift = mode == "drift"
    window = mode == "window"
    cost_observations = mode == "cost-observations"
    endpoint = os.getenv(
        "ORACLE_FORGE_DRIFT_API_URL" if drift else "ORACLE_FORGE_WINDOW_API_URL" if window
        else "ORACLE_FORGE_COST_OBSERVATIONS_API_URL" if cost_observations else "ORACLE_FORGE_API_URL",
        "http://127.0.0.1:8000/oracle/forge/drift" if drift
        else "http://127.0.0.1:8000/oracle/forge/window" if window
        else "http://127.0.0.1:8000/oracle/forge/cost-observations" if cost_observations
        else "http://127.0.0.1:8000/oracle/forge/experiment",
    )
    payload = {"symbol": symbol}
    if cost_observations:
        payload = {"symbols": list(symbols or [])}
    elif not drift and not window:
        payload.update({
            "reason": "scheduled Forge challenger review" if mode == "challenger"
            else f"scheduled Forge component ablation: {ablation}",
            "ablation_targets": [ablation] if ablation else [],
        })
        if isinstance(execution_costs, dict):
            payload["execution_costs"] = execution_costs
    request = urllib.request.Request(
        endpoint, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=2700 if cost_observations else 900) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096).decode("utf-8", errors="replace")
        except OSError:
            body = ""
        return {"status": "error", "error": "HTTPError", "http_status": exc.code,
                "reason": str(exc.reason), "response_body": _redact(body)}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"status": "error", "error": f"Oracle Forge request failed: {type(exc).__name__}"}


def main() -> int:
    now, cycle, scheduled_mode = _cycle()
    requested_mode = os.getenv("ORACLE_FORGE_EXPERIMENT_TYPE", "auto").strip().lower()
    if requested_mode == "auto":
        mode_spec = scheduled_mode
    elif requested_mode in ROTATION:
        mode_spec = requested_mode
    elif requested_mode == "ablation":
        target = os.getenv("ORACLE_FORGE_ABLATION_TARGET", "").strip().lower()
        if target not in ABLATIONS:
            raise SystemExit("An ablation run requires a registered ORACLE_FORGE_ABLATION_TARGET")
        mode_spec = f"ablation:{target}"
    else:
        raise SystemExit("Experiment type must be auto, challenger, drift, or ablation")
    mode = "ablation" if mode_spec.startswith("ablation:") else mode_spec
    ablation = mode_spec.split(":", 1)[1] if mode == "ablation" else None

    raw_symbols = os.getenv("ORACLE_FORGE_SYMBOLS", os.getenv("ORACLE_FORGE_SYMBOL", "ALL")).strip()
    symbols = list(DEFAULT_SYMBOLS) if raw_symbols.upper() in {"", "ALL", "DEFAULT"} else [
        item.strip().upper() for item in raw_symbols.split(",") if item.strip()
    ]
    if not symbols or any(not re.fullmatch(r"[A-Z0-9._-]{1,24}", symbol) for symbol in symbols):
        raise SystemExit("Oracle symbol list contains an invalid symbol")

    seen = _previous_windows()
    print(f"Collecting cTrader historical bid/ask ticks and deal costs for {len(symbols)} symbols", flush=True)
    cost_observation_result = _post("", "", "cost-observations", None, symbols=symbols)
    cost_data = cost_observation_result.get("data", {}) if isinstance(cost_observation_result, dict) else {}
    cost_profiles = cost_data.get("cost_profiles", {}) if isinstance(cost_data, dict) else {}
    if not isinstance(cost_profiles, dict):
        cost_profiles = {}
    results = []
    for index, symbol in enumerate(symbols, start=1):
        print(f"[{index}/{len(symbols)}] {symbol}: {mode_spec}", flush=True)
        fingerprint = None
        status = None
        execution_costs = cost_profiles.get(symbol)
        if mode == "cost-observations":
            observation = next((item for item in cost_data.get("observations", [])
                                if isinstance(item, dict) and item.get("symbol") == symbol), {})
            response = {"status": "complete" if isinstance(execution_costs, dict) else "blocked",
                        "data": {"status": "complete" if isinstance(execution_costs, dict) else "blocked",
                                 "cost_profile": execution_costs, "cost_observation": observation}}
            status = response["status"]
        elif mode != "drift" and not isinstance(execution_costs, dict):
            observation = next((item for item in cost_data.get("observations", [])
                                if isinstance(item, dict) and item.get("symbol") == symbol), {})
            response = {"status": "blocked", "data": {
                "status": "blocked", "message": "cTrader did not provide enough complete cost evidence for this symbol",
                "cost_observation": observation,
            }}
            status = "blocked"
        else:
            preflight = _post("", symbol, "window", None)
            preflight_data = preflight.get("data", {}) if isinstance(preflight, dict) else {}
            preflight_status = preflight.get("status") if isinstance(preflight, dict) else "error"
            if preflight_status != "complete" or not isinstance(preflight_data, dict) or not preflight_data.get("data_fingerprint"):
                response = {
                    "status": preflight_status or "error",
                    "data": preflight_data if isinstance(preflight_data, dict) else {},
                    "preflight_error": preflight.get("error") if isinstance(preflight, dict) else "invalid preflight response",
                    "http_status": preflight.get("http_status") if isinstance(preflight, dict) else None,
                    "response_body": preflight.get("response_body") if isinstance(preflight, dict) else None,
                }
                status = response["status"]
            else:
                fingerprint = preflight_data.get("data_fingerprint")
            if status is not None:
                pass
            elif fingerprint and (symbol, mode, ablation, fingerprint) in seen:
                response = {"status": "skipped", "data": {
                    "status": "skipped", "message": "this completed daily-data window was already assessed",
                    "data_fingerprint": fingerprint,
                }}
                status = "skipped"
            else:
                response = _post("", symbol, mode, ablation, execution_costs=execution_costs)
                status = response.get("status", "unknown") if isinstance(response, dict) else "unknown"
                if fingerprint and status == "complete":
                    seen.add((symbol, mode, ablation, fingerprint))
        results.append({
            "symbol": symbol, "experiment_type": mode,
            "ablation_target": ablation, "status": status,
            "data_fingerprint": fingerprint,
            "result": _redact(response),
        })

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORT_DIR / f"{stamp}-cycle-{cycle}-{mode_spec.replace(':', '-')}.json"
    report = {
        "schema": "forge.oracle.action_batch_report.v1",
        "recorded_at": now.isoformat(), "cycle_6h_utc": cycle,
        "rotation": list(ROTATION), "selected_mode": mode_spec,
        "symbol_count": len(symbols), "symbols": symbols,
        "research_only": True, "promotion_performed": False,
        "results": results,
        "broker_cost_observations": _redact(cost_observation_result),
        "generated_cost_profile_count": len(cost_profiles),
        "summary": {status: sum(item["status"] == status for item in results)
                    for status in sorted({item["status"] for item in results})},
        "workflow": "oracle-forge-experiment",
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report_path), "mode": mode_spec, "summary": report["summary"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
