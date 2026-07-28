import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import os
from execution.ctrader_broker import CTraderBroker
# ...rest of the script stays the same

# CTraderBroker() with no arguments automatically reads CTRADER_CLIENT_ID,
# CTRADER_CLIENT_SECRET, CTRADER_ACCESS_TOKEN, CTRADER_ACCOUNT_ID from your
# environment variables -- connect() itself takes no arguments.
broker = CTraderBroker()
result = broker.connect()
print(result)
print(broker.positions())