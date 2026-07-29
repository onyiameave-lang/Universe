import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import os
from execution.ctrader_broker import CTraderBroker
import time

from dotenv import load_dotenv
load_dotenv()


broker = CTraderBroker()
print("Connecting...")
print(broker.connect())
print()

# A small, minimal-size market order, no stop/target (avoids needing to
# guess at current price levels for this first test) -- just enough to
# prove place_order() actually works.
plan = {
    "approved": True,
    "symbol": "EURUSD",
    "direction": "long",
    "size": 1000,   # 1000 units = 0.01 standard lot, a small test size
}

print("Placing order:", plan)
result = broker.place_order(plan)
print("place_order() result:", result)
print()

if result.get("status") != "filled":
    print("Order did not fill -- stopping here, nothing to close.")
else:
    print("Waiting 2 seconds, then checking positions()...")
    time.sleep(2)
    positions = broker.positions()
    print("positions():", positions)
    print()

    ticket = result.get("order")
    if ticket:
        print(f"Closing position {ticket}...")
        close_result = broker.close_position(ticket)
        print("close_position() result:", close_result)
        print()
        print("positions() after close (should be empty):", broker.positions())