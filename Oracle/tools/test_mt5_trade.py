"""Safely smoke-test one minimum-volume MT5 demo/contest order and close it.

From the ecosystem root, run a diagnostic-only check with:
    python Oracle/tools/test_mt5_trade.py

To submit an order after reviewing the checks:
    python Oracle/tools/test_mt5_trade.py --execute
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

_ORACLE_ROOT = Path(__file__).resolve().parents[1]
_ECO_ROOT = _ORACLE_ROOT.parent
for _path in (_ORACLE_ROOT, _ECO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from shared.startup import load_dotenv_early

load_dotenv_early(__file__)

from execution.mt5_broker import MT5Broker  # noqa: E402


def _account_permissions(mt5: Any) -> Dict[str, Any]:
    terminal = mt5.terminal_info()
    account = mt5.account_info()
    if terminal is None or account is None:
        raise RuntimeError(f"MT5 permission check failed: {mt5.last_error()}")
    return {
        "terminal": terminal,
        "account": account,
        "terminal_trade_allowed": bool(getattr(terminal, "trade_allowed", False)),
        "terminal_tradeapi_disabled": bool(
            getattr(terminal, "tradeapi_disabled", True)),
        "account_trade_allowed": bool(getattr(account, "trade_allowed", False)),
        "account_trade_expert": bool(getattr(account, "trade_expert", False)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Place one minimum-volume MT5 demo order and immediately close it.")
    parser.add_argument("--symbol", default="EURUSD",
                        help="broker symbol to test (default: EURUSD)")
    parser.add_argument("--side", choices=("buy", "sell"), default="buy")
    parser.add_argument("--execute", action="store_true",
                        help="actually submit an order; otherwise only diagnose")
    args = parser.parse_args()
    symbol = args.symbol.strip().upper()
    if not symbol:
        parser.error("--symbol cannot be empty")

    broker = MT5Broker()
    try:
        status = broker.connect()
        print("Connection:", status)
        if not status.get("connected"):
            print("Stopped: MT5 connection failed.")
            return 1
        if broker.paper:
            print("Stopped: ORACLE_PAPER_TRADING is enabled; this test requires "
                  "the broker execution path.")
            return 1
        if status.get("account_type") not in {"demo", "contest"}:
            print("Stopped: this test is restricted to demo/contest accounts; "
                  f"detected {status.get('account_type')!r}.")
            return 1

        mt5 = broker._mt5
        permissions = _account_permissions(mt5)
        print(
            "Terminal permissions:",
            {
                key: value for key, value in permissions.items()
                if key.startswith("terminal_") or key.startswith("account_")
            },
        )
        if not permissions["terminal_trade_allowed"]:
            print("Stopped: MT5 terminal reports trade_allowed=False.")
            return 1
        if permissions["terminal_tradeapi_disabled"]:
            print("Stopped: MT5 terminal reports tradeapi_disabled=True.")
            return 1
        if not permissions["account_trade_allowed"] or not permissions["account_trade_expert"]:
            print("Stopped: MT5 account permissions disallow automated trading.")
            return 1

        if not mt5.symbol_select(symbol, True):
            print(f"Stopped: unable to select {symbol}: {mt5.last_error()}")
            return 1
        info = mt5.symbol_info(symbol)
        if info is None:
            print(f"Stopped: no symbol metadata for {symbol}: {mt5.last_error()}")
            return 1

        min_lots = float(getattr(info, "volume_min", 0.0))
        contract_size = float(getattr(info, "trade_contract_size", 0.0))
        volume_step = float(getattr(info, "volume_step", 0.0))
        if min_lots <= 0 or contract_size <= 0 or volume_step <= 0:
            print("Stopped: broker returned invalid minimum-volume/contract metadata.")
            return 1

        # Refuse to touch a pre-existing position; netting accounts can merge
        # a new order into it, making a test close unsafe.
        existing = broker.positions(symbol)
        if existing:
            print(f"Stopped: {symbol} already has open position(s): {existing}")
            return 1

        units = min_lots * contract_size
        volume = broker._clamp_volume(symbol, units)
        if volume is None or abs(volume - min_lots) > 1e-9:
            print(f"Stopped: could not safely prepare broker minimum {min_lots} lots.")
            return 1

        plan = {
            "approved": True,
            "symbol": symbol,
            "broker_symbol": symbol,
            "direction": args.side,
            "size": units,
            "stop": 0.0,
            "target": 0.0,
        }
        print(f"Prepared test: {args.side.upper()} {symbol}, {min_lots:g} lot(s), "
              "no SL/TP; the position will be closed immediately after fill.")
        if not args.execute:
            print("Dry run only. Add --execute to place the test order.")
            return 0

        confirmation = f"PLACE {args.side.upper()} {symbol}"
        typed = input(f"Type exactly '{confirmation}' to submit: ").strip()
        if typed != confirmation:
            print("Cancelled; no order submitted.")
            return 0

        result = broker.place_order(plan)
        print("place_order() result:", result)
        if result.get("status") != "filled":
            print("Order did not fill; no position will be closed.")
            return 1

        ticket: Optional[int] = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            positions = broker.positions(symbol)
            if positions:
                ticket = int(positions[0]["ticket"])
                break
            time.sleep(0.25)

        if ticket is None:
            print("URGENT: broker reported a fill but the new position could not "
                  "be located. Check MT5 and close the test position manually.")
            return 2

        print(f"Opened test position ticket={ticket}; closing it now.")
        close_result = broker.close_position(ticket)
        print("close_position() result:", close_result)
        if close_result.get("status") != "closed":
            print(f"URGENT: automatic close failed for ticket {ticket}. "
                  "Close it manually in MT5.")
            return 2

        print("Remaining positions for symbol:", broker.positions(symbol))
        print("MT5 test order was placed and close was confirmed.")
        return 0
    finally:
        broker.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
