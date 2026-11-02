"""Run a bounded, demo-only cTrader fill sample and close every test position.

This is for execution-cost evidence collection, not a trading strategy. Each
round trip creates one market entry and closes that same position. The script
refuses to start if the selected symbol already has an open position, and it
stops immediately if a position cannot be confirmed closed.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=False)

from Oracle.execution.ctrader_broker import CTraderBroker


def _positions_for_symbol(broker: CTraderBroker, symbol: str) -> list[dict]:
    return [item for item in broker.positions()
            if str(item.get("symbol", "")).strip().upper() == symbol]


def _position_by_ticket(broker: CTraderBroker, ticket: int) -> dict | None:
    return next((item for item in broker.positions()
                 if str(item.get("ticket")) == str(ticket)), None)


def _close_and_verify(broker: CTraderBroker, ticket: int) -> bool:
    """Close once, retry only if a fresh position read proves it is still open."""
    for attempt in range(2):
        if _position_by_ticket(broker, ticket) is None:
            return True
        result = broker.close_position(ticket)
        print(f"Close attempt {attempt + 1}: {result}", flush=True)
        if _position_by_ticket(broker, ticket) is None:
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect a small cTrader demo fill sample")
    parser.add_argument("--symbol", default="EURUSD", help="Exact broker symbol, e.g. GBPUSD")
    parser.add_argument("--round-trips", type=int, default=1,
                        help="Number of entry/close pairs (maximum 10)")
    parser.add_argument("--units", type=int, default=1000,
                        help="Order size in units; 1000 is approximately 0.01 lot on FX")
    parser.add_argument("--hold-seconds", type=float, default=2.0,
                        help="Seconds to wait between fill and close")
    args = parser.parse_args()

    symbol = args.symbol.strip().upper()
    if not symbol or args.round_trips < 1 or args.round_trips > 10:
        parser.error("provide a symbol and 1-10 round trips")
    if args.units < 1 or args.units > 1000:
        parser.error("units must be between 1 and 1000 for this diagnostic")
    if not 0 <= args.hold_seconds <= 10:
        parser.error("hold-seconds must be between 0 and 10")

    broker = CTraderBroker(use_demo=True)
    connection = broker.connect()
    if not connection.get("connected") or connection.get("account_type") != "demo":
        print("STOP: cTrader demo connection was not confirmed; no orders submitted.", flush=True)
        return 2
    print("Connected to cTrader demo endpoint.", flush=True)

    existing = _positions_for_symbol(broker, symbol)
    if existing:
        print(f"STOP: {len(existing)} {symbol} position(s) already open; no orders submitted.", flush=True)
        return 2

    completed = 0
    for index in range(1, args.round_trips + 1):
        known_tickets = {str(item.get("ticket")) for item in broker.positions()}
        ticket = None
        entry_failed = False
        cleanup_error = None
        print(f"Round trip {index}/{args.round_trips}: placing {args.units} {symbol} units.", flush=True)
        try:
            opened = broker.place_order({
                "approved": True, "symbol": symbol,
                "direction": "long", "size": args.units,
            })
            print("Entry result:", opened, flush=True)
            order_ticket = opened.get("order") if isinstance(opened, dict) else None
            if opened.get("status") == "filled" and order_ticket is not None:
                ticket = int(order_ticket)
            else:
                # An execution timeout can hide a successful fill. Adopt only
                # one newly observed position on this symbol; ambiguity stops.
                new_positions = [item for item in _positions_for_symbol(broker, symbol)
                                 if str(item.get("ticket")) not in known_tickets]
                if len(new_positions) == 1:
                    ticket = int(new_positions[0]["ticket"])
                else:
                    print("STOP: entry was not confirmed and no unique new position was found.",
                          flush=True)
                    entry_failed = True

            if not entry_failed:
                time.sleep(args.hold_seconds)
        except Exception as exc:
            print(f"STOP: entry handling failed ({type(exc).__name__}).", flush=True)
            entry_failed = True
        finally:
            if ticket is None:
                # Also inspect after an exception or ambiguous execution
                # response, since the broker may have filled before the
                # client lost its acknowledgement.
                try:
                    new_positions = [item for item in _positions_for_symbol(broker, symbol)
                                     if str(item.get("ticket")) not in known_tickets]
                    if len(new_positions) == 1:
                        ticket = int(new_positions[0]["ticket"])
                    elif new_positions:
                        cleanup_error = (
                            f"found {len(new_positions)} uncorrelated new {symbol} positions; "
                            "stopped without guessing which position to close"
                        )
                except Exception as exc:
                    cleanup_error = f"could not inspect {symbol} positions after entry: {type(exc).__name__}"
            if ticket is not None:
                try:
                    if not _close_and_verify(broker, ticket):
                        cleanup_error = f"could not verify {symbol} position {ticket} closed"
                except Exception as exc:
                    cleanup_error = f"could not close {symbol} position {ticket}: {type(exc).__name__}"

        if cleanup_error:
            print(f"CRITICAL: {cleanup_error}; stopping batch.", flush=True)
            return 4
        if entry_failed:
            return 3

        completed += 1
        print(f"Round trip {index} closed and verified.", flush=True)

    remaining = _positions_for_symbol(broker, symbol)
    if remaining:
        print(f"STOP: {len(remaining)} {symbol} position(s) remain open.", flush=True)
        return 4
    print(f"Complete: {completed} demo round trip(s); no {symbol} positions remain open.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
