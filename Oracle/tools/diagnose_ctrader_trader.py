import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import os
from execution.ctrader_broker import CTraderBroker

broker = CTraderBroker()
result = broker.connect()
print("connect() result:", result)
print()

# Manually send the trader request again and print the RAW payload,
# so we can see the actual field names instead of guessing.
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOATraderReq

req = ProtoOATraderReq()
req.ctidTraderAccountId = broker._account_id
payload = broker._send_and_wait(req, timeout=10)

print("Raw payload type:", type(payload))
print()
print("Raw payload contents:")
print(payload)
print()
print("Top-level fields available:", [f.name for f, _ in payload.ListFields()])