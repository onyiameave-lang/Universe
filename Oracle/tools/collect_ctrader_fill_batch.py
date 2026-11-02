"""Preflight and collect a bounded cTrader demo fill batch for cost research.

The default is a read-only dry run. Execution uses broker minimum volume by
default, checks actual fill-count deficits, closes each test position, and has
both per-symbol and batch round-trip limits.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=False)

from Oracle.execution.ctrader_broker import CTraderBroker
from Oracle.execution.ctrader_demo_trader import DEFAULT_SYMBOLS


def _positions_for_symbol(broker: CTraderBroker, symbol: str) -> list[dict]:
    return [item for item in broker.positions()
            if str(item.get("symbol", "")).strip().upper() == symbol]


def _position_by_ticket(broker: CTraderBroker, ticket: int) -> dict | None:
    return next((item for item in broker.positions()
                 if str(item.get("ticket")) == str(ticket)), None)


def _close_and_verify(broker: CTraderBroker, ticket: int) -> bool:
    for attempt in range(2):
        if _position_by_ticket(broker, ticket) is None:
            return True
        result = broker.close_position(ticket)
        print(f"Close attempt {attempt + 1}: {result}", flush=True)
        if _position_by_ticket(broker, ticket) is None:
            return True
    return False


def _read_fill_counts(broker: CTraderBroker, days: int) -> dict[str, int] | None:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    history = broker.historical_deals(start_ms, now_ms, max_rows=10000, timeout=60.0)
    if history.get("status") != "complete":
        print("STOP: cTrader did not return complete deal history; no fill counts or orders.", flush=True)
        return None
    counts: dict[str, int] = {}
    for deal in history.get("deals", []):
        if (deal.get("deal_status") in {2, 3}
                and float(deal.get("filled_volume_cents") or 0) > 0):
            name = str(deal.get("symbol", "")).upper()
            counts[name] = counts.get(name, 0) + 1
    return counts


def _load_symbol_aliases() -> dict[str, str]:
    path = ROOT / "Oracle" / "config" / "ctrader_research_symbol_aliases.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    aliases = document.get("aliases", {}) if isinstance(document, dict) else {}
    return {str(key).upper(): str(value).upper() for key, value in aliases.items()}


def _selected_symbols(args) -> list[str]:
    if args.all_symbols:
        raw = DEFAULT_SYMBOLS
    elif args.symbols:
        raw = [item.strip() for item in args.symbols.split(",") if item.strip()]
    elif args.symbol:
        raw = [args.symbol]
    else:
        raw = ["EURUSD"]
    return list(dict.fromkeys(str(item).strip().upper() for item in raw if str(item).strip()))


def _build_plans(broker, symbols, aliases, counts, args):
    catalog = set(broker.symbol_catalog())
    existing = broker.positions()
    if not getattr(broker, "_positions_last_refresh_ok", True):
        raise RuntimeError("cTrader could not confirm open positions; refusing the batch")
    open_symbols = {str(item.get("symbol", "")).upper() for item in existing}
    plans, skipped = [], []
    remaining_batch = args.max_total_round_trips
    for symbol in symbols:
        broker_symbol = aliases.get(symbol, symbol)
        if broker_symbol not in catalog:
            skipped.append((symbol, f"mapped broker symbol {broker_symbol} is not listed"))
            continue
        if broker_symbol in open_symbols:
            skipped.append((symbol, "an open position already exists"))
            continue
        try:
            metadata = broker.symbol_metadata(broker_symbol)
            min_volume = int(metadata["minVolume"])
            max_volume = int(metadata["maxVolume"])
            step_volume = max(1, int(metadata["stepVolume"]))
            if metadata.get("digits") is None or min_volume <= 0:
                raise ValueError("broker size or price metadata is incomplete")
            min_units = min_volume / 100.0
            max_units = max_volume / 100.0
            units = min_units if args.units is None else float(args.units)
            cents = int(round(units * 100))
            if units > args.max_units:
                lot_size = metadata.get("lotSize")
                raise ValueError(
                    f"broker minimum is {min_units:g} units "
                    f"(minVolume={min_volume}, maxVolume={max_volume}, lotSize={lot_size}); "
                    f"it exceeds the {args.max_units:g}-unit safety cap. "
                    "Treat this metadata as unverified; do not raise the cap to force an order."
                )
            if cents < min_volume or cents > max_volume:
                raise ValueError(f"size {units:g} is outside broker limits {min_units:g}-{max_units:g} units")
            if (cents - min_volume) % step_volume:
                raise ValueError("size does not match the broker's volume step")
        except Exception as exc:
            skipped.append((symbol, f"size preflight failed: {type(exc).__name__}: {exc}"))
            continue

        fills = counts.get(broker_symbol, 0)
        needed = max(0, args.target_fills - fills)
        needed_round_trips = math.ceil(needed / 2)
        per_symbol = min(needed_round_trips, args.max_round_trips_per_symbol)
        round_trips = min(per_symbol, remaining_batch)
        remaining_batch -= round_trips
        plans.append({
            "symbol": symbol, "broker_symbol": broker_symbol, "units": units,
            "fills": fills, "fills_needed": needed,
            "round_trips": round_trips,
            "round_trips_needed": needed_round_trips,
        })
    return plans, skipped


def _execute_plan(broker, plan, hold_seconds: float) -> tuple[int, bool]:
    completed = 0
    for index in range(1, plan["round_trips"] + 1):
        symbol = plan["broker_symbol"]
        known_tickets = {str(item.get("ticket")) for item in broker.positions()}
        ticket = None
        opened = broker.place_order({
            "approved": True, "symbol": symbol,
            "direction": "long", "size": plan["units"],
        })
        print(f"{symbol} round trip {index}/{plan['round_trips']}: {opened}", flush=True)
        if isinstance(opened, dict) and opened.get("status") == "filled" and opened.get("order") is not None:
            ticket = int(opened["order"])
        else:
            new_positions = [item for item in _positions_for_symbol(broker, symbol)
                             if str(item.get("ticket")) not in known_tickets]
            if len(new_positions) == 1:
                ticket = int(new_positions[0]["ticket"])
            elif len(new_positions) > 1:
                print(f"CRITICAL: {len(new_positions)} new {symbol} positions; stopping batch.", flush=True)
                return completed, True
            elif isinstance(opened, dict) and opened.get("status") == "rejected":
                print(f"Skipping remaining {symbol} attempts after a broker rejection.", flush=True)
                return completed, False
            else:
                print(f"CRITICAL: execution result for {symbol} is uncertain; stopping batch.", flush=True)
                return completed, True
        time.sleep(hold_seconds)
        if not _close_and_verify(broker, ticket):
            print(f"CRITICAL: could not verify {symbol} position {ticket} closed; stopping batch.", flush=True)
            return completed, True
        completed += 1
    return completed, False


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight or run a bounded cTrader demo fill batch")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--symbol", help="One Oracle symbol, e.g. EURUSD")
    group.add_argument("--symbols", help="Comma-separated Oracle symbols")
    group.add_argument("--all-symbols", action="store_true", help="Use Oracle's configured default symbols")
    parser.add_argument("--target-fills", type=int, default=20,
                        help="Actual filled-deal target per symbol (default: 20)")
    parser.add_argument("--units", type=float, default=None,
                        help="Explicit units; default uses each broker symbol's minimum size")
    parser.add_argument("--max-units", type=float, default=1000,
                        help="Hard per-order unit cap (default: 1000)")
    parser.add_argument("--max-round-trips-per-symbol", type=int, default=10)
    parser.add_argument("--max-total-round-trips", type=int, default=10,
                        help="Maximum round trips in one batch (default: 10)")
    parser.add_argument("--deal-history-days", type=int, default=365,
                        help="Deal-history lookback for fill counts (default: 365 days)")
    parser.add_argument("--hold-seconds", type=float, default=2.0)
    parser.add_argument("--execute", action="store_true",
                        help="Submit demo orders; without this flag the command is read-only")
    args = parser.parse_args()
    if (args.target_fills < 1 or not 1 <= args.max_round_trips_per_symbol <= 10
            or not 1 <= args.max_total_round_trips <= 100
            or not 7 <= args.deal_history_days <= 3650
            or not 0 <= args.hold_seconds <= 10
            or args.max_units <= 0
            or (args.units is not None and args.units <= 0)):
        parser.error("fill targets, size, and batch limits are outside their safe ranges")

    broker = CTraderBroker(use_demo=True)
    connection = broker.connect()
    if (not connection.get("connected")
            or connection.get("account_type") != "demo"
            or not connection.get("account_info_available")):
        reason = connection.get("reason") or "demo account details were not confirmed"
        print(f"STOP: cTrader demo account is not fully authenticated ({reason}); "
              "no history counts or orders were used/submitted.", flush=True)
        return 2
    print("Connected to cTrader demo endpoint.", flush=True)

    counts = _read_fill_counts(broker, args.deal_history_days)
    if counts is None:
        return 2
    try:
        plans, skipped = _build_plans(broker, _selected_symbols(args), _load_symbol_aliases(), counts, args)
    except Exception as exc:
        print(f"STOP: batch preflight failed ({type(exc).__name__}); no orders submitted.", flush=True)
        return 2

    for plan in plans:
        print(
            f"PLAN {plan['symbol']} -> {plan['broker_symbol']}: units={plan['units']:g}, "
            f"fills={plan['fills']}/{args.target_fills}, need={plan['fills_needed']}, "
            f"round_trips_needed={plan['round_trips_needed']}, "
            f"this_batch={plan['round_trips']}", flush=True,
        )
    for symbol, reason in skipped:
        print(f"SKIP {symbol}: {reason}", flush=True)
    if not args.execute:
        print("Dry run only: no orders were submitted. Add --execute to run this bounded batch.", flush=True)
        return 0

    completed_total = 0
    for plan in plans:
        if plan["round_trips"] <= 0:
            continue
        completed, critical = _execute_plan(broker, plan, args.hold_seconds)
        completed_total += completed
        if critical:
            return 4
    print(f"Batch complete: {completed_total} demo round trip(s); target remains {args.target_fills} fills per symbol.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
