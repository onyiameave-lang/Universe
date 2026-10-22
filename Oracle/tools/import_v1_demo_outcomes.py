"""Preview or import V1 demo trade outcomes into the local V2 Forge instance.

The V1 JSONL file is read-only. Imports are dry-run by default; pass --apply
to send identified demo outcomes to the loopback-only V2 API endpoint.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


DEFAULT_SOURCE = (
    Path(__file__).resolve().parents[4]
    / "Universe" / "Universal_AI" / "Oracle" / "memory" / "trade_experiment_log.jsonl"
)
DEFAULT_ENDPOINT = "http://127.0.0.1:8000/oracle/forge/import-v1-demo-outcome"


def normalize_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    streams = row.get("entry_streams")
    streams = streams if isinstance(streams, dict) else {}
    technical = streams.get("technical")
    technical = technical if isinstance(technical, dict) else {}
    strategy_id = str(row.get("strategy_id") or technical.get("genome") or "").strip()
    symbol = str(row.get("symbol") or "").strip().upper()
    regime = str(row.get("entry_regime") or technical.get("regime") or "unknown").strip()
    try:
        pnl_r = float(row["pnl_r"])
        source_timestamp = float(row["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None
    if (not strategy_id or not symbol or not regime or not math.isfinite(pnl_r)
            or not math.isfinite(source_timestamp)):
        return None
    canonical = json.dumps(row, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False).encode("utf-8")
    close_event_id = "oracle-v1-jsonl:" + hashlib.sha256(canonical).hexdigest()
    return {
        "schema": "oracle.strategy.demo_outcome.v1",
        "close_event_id": close_event_id,
        "strategy_id": strategy_id,
        "timeframe_policy_id": row.get("timeframe_policy_id"),
        "symbol": symbol,
        "regime": regime,
        "execution_environment": "demo",
        "pnl_r": pnl_r,
        "won": pnl_r > 0,
        "closed_at_unix": source_timestamp,
        "exit_reason": str(row.get("exit_reason", "v1_imported_outcome")),
        "feedback_source": "oracle_v1_demo_import",
        "pnl_basis": "V1 price return in initial-stop R; broker costs unavailable in source row",
        "cost_status": "unavailable_provisional",
    }


def _post(endpoint: str, outcome: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    body = json.dumps({"outcome": outcome}, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("V2 API returned a non-object response")
    return payload


def _rows(source: Path) -> Iterable[tuple[int, Dict[str, Any]]]:
    with source.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield line_number, row


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview/import V1 demo outcomes into V2 Forge")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                        help="V1 trade_experiment_log.jsonl (read-only)")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                        help="V2 local API import endpoint")
    parser.add_argument("--apply", action="store_true",
                        help="write eligible demo outcomes into V2 Chronicle through local API")
    parser.add_argument("--limit", type=int, default=0,
                        help="maximum eligible outcomes to process; 0 means all")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    if not args.source.is_file():
        parser.error(f"V1 outcome file not found: {args.source}")
    eligible = imported = skipped = failed = 0
    for line_number, row in _rows(args.source):
        try:
            outcome = normalize_row(row)
        except (TypeError, ValueError):
            outcome = None
        if outcome is None:
            skipped += 1
            continue
        if args.limit and eligible >= args.limit:
            break
        eligible += 1
        if not args.apply:
            continue
        try:
            result = _post(args.endpoint, outcome, args.timeout)
            data = result.get("data") if isinstance(result.get("data"), dict) else result
            if result.get("status") == "complete" or data.get("status") == "complete":
                imported += 1
            else:
                failed += 1
                print(f"line {line_number}: Forge did not accept event ({data.get('message', data.get('status', 'unknown'))})")
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            failed += 1
            print(f"line {line_number}: import failed ({type(exc).__name__})")

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"{mode}: eligible={eligible}, imported={imported}, skipped_unidentified_or_invalid={skipped}, failed={failed}")
    if not args.apply:
        print("No Chronicle records were written. Re-run with --apply after V2 API and Forge are running.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
