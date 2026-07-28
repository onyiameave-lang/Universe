
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import os
from execution.ctrader_broker import CTraderBroker

from dotenv import load_dotenv
load_dotenv()

broker = CTraderBroker()
print("Connecting...")
print(broker.connect())
print()

positions = broker.positions()
print(f"Found {len(positions)} open position(s):")
for p in positions:
    print(" ", p)
print()

if not positions:
    print("Nothing to close.")
else:
    for p in positions:
        ticket = p["ticket"]
        print(f"Closing position {ticket} ({p['symbol']} {p['type']} {p['volume']})...")
        result = broker.close_position(ticket)
        print("  ->", result)

    print()
    print("Re-checking positions() after closing (should be empty):")
    print(broker.positions())