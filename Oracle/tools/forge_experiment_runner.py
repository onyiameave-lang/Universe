"""Run one bounded Oracle/Forge research mode over the configured watchlist.

Scheduled runs rotate one mode per six-hour UTC slot and process symbols
sequentially. This is research-only: it never places orders or promotes a
strategy. Completed seven-timeframe data windows are not evaluated twice.
"""
from __future__ import annotations

import json
import hashlib
import math
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
REPORT_DIR = ROOT / "Chronicle" / "research" / "oracle_forge_experiments"
LEGACY_REPORT_DIR = ROOT / "Forge" / "reports" / "oracle_experiments"
CATALOG_DIR = ROOT / "Chronicle" / "research" / "oracle_strategy_catalogs"
COST_PROFILE_FILE = ROOT / "Oracle" / "config" / "forge_cost_profiles.json"
SYMBOL_ALIASES_FILE = ROOT / "Oracle" / "config" / "ctrader_research_symbol_aliases.json"


def _load_manual_profiles():
    try:
        document = json.loads(COST_PROFILE_FILE.read_text(encoding="utf-8"))
        aliases = json.loads(SYMBOL_ALIASES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot load versioned manual cost profiles: {type(exc).__name__}")
    if document.get("schema") != "oracle.forge.manual_cost_profiles.v1":
        raise SystemExit("Manual cost profile schema is missing or unsupported")
    if aliases.get("schema") != "oracle.ctrader_research_symbol_aliases.v1":
        raise SystemExit("cTrader research symbol alias schema is missing or unsupported")
    profiles = document.get("profiles")
    alias_map = aliases.get("aliases")
    if not isinstance(profiles, dict) or not isinstance(alias_map, dict):
        raise SystemExit("Manual cost profiles or symbol aliases are malformed")
    return document, profiles, {str(k).upper(): str(v).upper() for k, v in alias_map.items()}


def _manual_profile_for(symbol, reference_price, document, profiles, aliases):
    broker_symbol = aliases.get(symbol, symbol)
    raw = profiles.get(broker_symbol)
    if not isinstance(raw, dict):
        return None
    if isinstance(reference_price, bool) or not isinstance(reference_price, (int, float)) or reference_price <= 0:
        return None
    try:
        spread = float(raw["average_spread_price_units"]) / float(reference_price) * 10000.0
        slippage = float(raw["estimated_slippage_price_units_per_side"]) / float(reference_price) * 10000.0
        stress_spread = float(raw["stress_spread_price_units"]) / float(reference_price) * 10000.0
        stress_slippage = float(raw["stress_slippage_price_units_per_side"]) / float(reference_price) * 10000.0
        commission = float(raw["commission_bps_per_side"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    values = (spread, slippage, stress_spread, stress_slippage, commission)
    if any(value < 0 or not math.isfinite(value) for value in values):
        return None
    return {
        "broker": "Pepperstone",
        "account_type": "cTrader Demo, commission-based; Razor pricing profile assumed",
        "profile_version": str(raw["profile_version"]),
        "profile_status": "published_spread_estimated_slippage_research_only",
        "promotion_eligible": False,
        "price_basis": "bid",
        "commission_bps": commission,
        "slippage_bps": slippage,
        "spread_bps": spread,
        "stress_slippage_bps": stress_slippage,
        "stress_spread_bps": stress_spread,
        "commission_source": raw["commission_source"],
        "spread_source": raw["spread_source"],
        "slippage_source": raw["slippage_source"],
        "manual_cost_data_version": document["data_version"],
        "manual_cost_source_period": document["published_spread_period"],
        "manual_cost_sources": document.get("sources", {}),
        "manual_cost_broker_symbol": broker_symbol,
        "manual_cost_unit_conversion": {
            "spread_and_slippage_source_units": raw["unit"],
            "reference_price": float(reference_price),
            "conversion": "price_units / latest_completed_bid_close * 10000",
        },
    }


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
    for directory in (REPORT_DIR, LEGACY_REPORT_DIR):
        for path in directory.glob("*.json"):
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for item in report.get("results", []):
                fingerprint = item.get("data_fingerprint")
                if fingerprint and item.get("status") == "complete":
                    seen.add((item.get("symbol"), item.get("experiment_type"), item.get("ablation_target"),
                              fingerprint, item.get("cost_profile_version")))
    return seen


def _persist_catalog_artifact(sync_result):
    """Write a content-addressed catalog under Chronicle's durable research area."""
    data = sync_result.get("data", {}) if isinstance(sync_result, dict) else {}
    catalog = data.get("catalog_snapshot") if isinstance(data, dict) else None
    if not isinstance(catalog, dict) or catalog.get("schema") != "oracle.strategy_catalog.snapshot.v1":
        return {"status": "blocked", "message": "Chronicle catalog snapshot was not returned"}
    fingerprint = catalog.get("catalog_fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        return {"status": "blocked", "message": "catalog fingerprint is invalid"}
    canonical = {key: value for key, value in catalog.items()
                 if key not in {"catalog_fingerprint", "record_count",
                                "family_template_count", "source_error_count"}}
    try:
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return {"status": "blocked", "message": "catalog contains non-canonical values"}
    if hashlib.sha256(encoded).hexdigest() != fingerprint:
        return {"status": "blocked", "message": "catalog fingerprint does not match its contents"}
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    artifact = CATALOG_DIR / f"{fingerprint}.json"
    if artifact.exists():
        try:
            existing = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"status": "blocked", "message": "existing Chronicle catalog artifact is unreadable"}
        existing_body = {key: value for key, value in existing.items()
                         if key not in {"catalog_fingerprint", "record_count",
                                        "family_template_count", "source_error_count"}}
        existing_encoded = json.dumps(existing_body, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False, allow_nan=False).encode("utf-8")
        if (existing.get("catalog_fingerprint") != fingerprint
                or hashlib.sha256(existing_encoded).hexdigest() != fingerprint):
            return {"status": "blocked", "message": "catalog artifact fingerprint does not match its content"}
        return {"status": "complete", "catalog_fingerprint": fingerprint,
                "record_count": len(catalog.get("records", [])), "already_saved": True}
    temporary = artifact.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(artifact)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        return {"status": "blocked", "message": f"could not persist catalog artifact: {type(exc).__name__}"}
    return {"status": "complete", "catalog_fingerprint": fingerprint,
            "record_count": len(catalog.get("records", [])), "already_saved": False}


def _post(url: str, symbol: str, mode: str, ablation: str | None, symbols=None,
          execution_costs=None):
    drift = mode == "drift"
    window = mode == "window"
    cost_observations = mode == "cost-observations"
    catalog_sync = mode == "catalog-sync"
    endpoint = os.getenv(
        "ORACLE_FORGE_CATALOG_API_URL" if catalog_sync
        else "ORACLE_FORGE_DRIFT_API_URL" if drift else "ORACLE_FORGE_WINDOW_API_URL" if window
        else "ORACLE_FORGE_COST_OBSERVATIONS_API_URL" if cost_observations else "ORACLE_FORGE_API_URL",
        "http://127.0.0.1:8000/oracle/forge/catalog" if catalog_sync
        else "http://127.0.0.1:8000/oracle/forge/drift" if drift
        else "http://127.0.0.1:8000/oracle/forge/window" if window
        else "http://127.0.0.1:8000/oracle/forge/cost-observations" if cost_observations
        else "http://127.0.0.1:8000/oracle/forge/experiment",
    )
    payload = {"symbol": symbol}
    if catalog_sync:
        payload = {}
    elif cost_observations:
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
        with urllib.request.urlopen(request, timeout=2700) as response:
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
    collect_empirical = os.getenv("ORACLE_FORGE_COLLECT_EMPIRICAL_COSTS", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if collect_empirical or mode == "cost-observations":
        print(f"Collecting cTrader cost observations for {len(symbols)} symbols", flush=True)
        cost_observation_result = _post("", "", "cost-observations", None, symbols=symbols)
    else:
        print("Using versioned manual cost profiles; cTrader fill-history collection is disabled", flush=True)
        cost_observation_result = {"status": "skipped", "data": {
            "status": "skipped", "message": "empirical cost collection disabled for this cycle",
            "observations": [], "cost_profiles": {},
        }}
    print("Syncing Oracle strategy catalog to Chronicle through Forge", flush=True)
    catalog_sync_result = _post("", "", "catalog-sync", None)
    catalog_sync_data = catalog_sync_result.get("data", {}) if isinstance(catalog_sync_result, dict) else {}
    catalog_store_result = _persist_catalog_artifact(catalog_sync_result)
    catalog_sync_ok = (
        isinstance(catalog_sync_result, dict)
        and catalog_sync_result.get("status") == "complete"
        and isinstance(catalog_sync_data, dict)
        and catalog_sync_data.get("status") == "complete"
        and catalog_store_result.get("status") == "complete"
    )
    catalog_sync_summary = {
        "status": "complete" if catalog_sync_ok else "blocked",
        "catalog_fingerprint": catalog_store_result.get("catalog_fingerprint"),
        "record_count": catalog_store_result.get("record_count"),
        "already_saved": catalog_store_result.get("already_saved"),
        "artifact_status": catalog_store_result.get("status"),
        "artifact_message": catalog_store_result.get("message"),
        "chronicle_state_status": catalog_sync_data.get("status") if isinstance(catalog_sync_data, dict) else None,
        "chronicle_state_message": catalog_sync_data.get("message") if isinstance(catalog_sync_data, dict) else None,
    }
    cost_data = cost_observation_result.get("data", {}) if isinstance(cost_observation_result, dict) else {}
    cost_profiles = cost_data.get("cost_profiles", {}) if isinstance(cost_data, dict) else {}
    if not isinstance(cost_profiles, dict):
        cost_profiles = {}
    cost_document, manual_profiles, aliases = _load_manual_profiles()
    results = []
    for index, symbol in enumerate(symbols, start=1):
        print(f"[{index}/{len(symbols)}] {symbol}: {mode_spec}", flush=True)
        fingerprint = None
        status = None
        timeframe_coverage = None
        timeframe_fingerprints = None
        execution_costs = cost_profiles.get(symbol)
        cost_profile_source = "ctrader_observed" if isinstance(execution_costs, dict) else None
        if mode == "cost-observations":
            observation = next((item for item in cost_data.get("observations", [])
                                if isinstance(item, dict) and item.get("symbol") == symbol), {})
            response = {"status": "complete" if isinstance(execution_costs, dict) else "blocked",
                        "data": {"status": "complete" if isinstance(execution_costs, dict) else "blocked",
                                 "cost_profile": execution_costs, "cost_observation": observation}}
            status = response["status"]
        elif mode not in {"drift", "cost-observations"} and not catalog_sync_ok:
            response = {"status": "blocked", "data": {
                "status": "blocked",
                "message": "Oracle strategy catalog was not durably persisted to Chronicle; refusing strategy research",
                "catalog_sync": catalog_sync_summary,
            }}
            status = "blocked"
        elif mode == "drift":
            response = _post("", symbol, "drift", None)
            drift_data = response.get("data", {}) if isinstance(response, dict) else {}
            fingerprint = drift_data.get("window_fingerprint") if isinstance(drift_data, dict) else None
            status = response.get("status", "unknown") if isinstance(response, dict) else "unknown"
            if fingerprint and status == "complete":
                drift_key = (symbol, mode, None, fingerprint, None)
                if drift_key in seen:
                    response = {"status": "skipped", "data": {
                        "status": "skipped", "message": "this completed daily drift window was already assessed",
                        "data_fingerprint": fingerprint,
                    }}
                    status = "skipped"
                else:
                    seen.add(drift_key)
            results.append({
                "symbol": symbol, "experiment_type": mode,
                "ablation_target": None, "status": status,
                "cost_profile_source": None, "cost_profile_version": None,
                "execution_costs": None, "data_fingerprint": fingerprint,
                "timeframe_fingerprints": None, "timeframe_coverage": None,
                "result": _redact(response),
            })
            continue
        elif (mode != "drift" and not isinstance(execution_costs, dict)
              and aliases.get(symbol, symbol) not in manual_profiles):
            response = {"status": "blocked", "data": {
                "status": "blocked",
                "message": "no published manual cost profile for this symbol; enable empirical cTrader cost collection after obtaining sufficient valid fills",
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
                timeframe_coverage = preflight_data.get("timeframe_coverage")
                timeframe_fingerprints = preflight_data.get("timeframe_fingerprints")
                if mode != "drift" and not isinstance(execution_costs, dict):
                    execution_costs = _manual_profile_for(
                        symbol, preflight_data.get("latest_close"),
                        cost_document, manual_profiles, aliases,
                    )
                    if isinstance(execution_costs, dict):
                        cost_profile_source = "published_broker_costs_plus_modeled_slippage"
            if status is not None:
                pass
            elif mode != "drift" and not isinstance(execution_costs, dict):
                observation = next((item for item in cost_data.get("observations", [])
                                    if isinstance(item, dict) and item.get("symbol") == symbol), {})
                response = {"status": "blocked", "data": {
                    "status": "blocked",
                    "message": "no valid cTrader cost observations or published manual cost profile for this symbol",
                    "cost_observation": observation,
                }}
                status = "blocked"
            elif fingerprint and (symbol, mode, ablation, fingerprint,
                                  execution_costs.get("profile_version") if isinstance(execution_costs, dict) else None) in seen:
                response = {"status": "skipped", "data": {
                    "status": "skipped", "message": "this completed daily-data window was already assessed",
                    "data_fingerprint": fingerprint,
                }}
                status = "skipped"
            else:
                response = _post("", symbol, mode, ablation, execution_costs=execution_costs)
                status = response.get("status", "unknown") if isinstance(response, dict) else "unknown"
                if fingerprint and status == "complete":
                    seen.add((symbol, mode, ablation, fingerprint,
                              execution_costs.get("profile_version") if isinstance(execution_costs, dict) else None))
        results.append({
            "symbol": symbol, "experiment_type": mode,
            "ablation_target": ablation, "status": status,
            "cost_profile_source": cost_profile_source,
            "cost_profile_version": execution_costs.get("profile_version") if isinstance(execution_costs, dict) else None,
            "execution_costs": _redact(execution_costs),
            "data_fingerprint": fingerprint,
            "timeframe_fingerprints": timeframe_fingerprints,
            "timeframe_coverage": timeframe_coverage,
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
        "strategy_catalog_sync": catalog_sync_summary,
        "broker_cost_observations": _redact(cost_observation_result),
        "generated_cost_profile_count": len(cost_profiles),
        "manual_cost_profile_version": cost_document.get("data_version"),
        "manual_cost_profile_count": len(manual_profiles),
        "manual_cost_watchlist_coverage_count": sum(
            1 for symbol in symbols if aliases.get(symbol, symbol) in manual_profiles
        ),
        "empirical_cost_collection_enabled": collect_empirical or mode == "cost-observations",
        "summary": {status: sum(item["status"] == status for item in results)
                    for status in sorted({item["status"] for item in results})},
        "workflow": "oracle-forge-experiment",
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report_path), "mode": mode_spec, "summary": report["summary"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
