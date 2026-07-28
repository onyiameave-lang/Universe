import time
from execution.ctrader_broker import CTraderBroker
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAReconcileReq

broker = CTraderBroker()
print("Connecting...")
print(broker.connect())
print()

plan = {"approved": True, "symbol": "EURUSD", "direction": "long", "size": 1000}
print("Placing a fresh test order:", plan)
result = broker.place_order(plan)
print("place_order() result:", result)
print()

print("Waiting 3 seconds before reconciling...")
time.sleep(3)

req = ProtoOAReconcileReq()
req.ctidTraderAccountId = broker._account_id
payload = broker._send_and_wait(req)

print("Raw ReconcileRes payload type:", type(payload))
print()
print("Raw ReconcileRes contents:")
print(payload)
print()
if payload is not None:
    print("Top-level fields available:", [f.name for f, _ in payload.ListFields()])

print()
print("!!! IMPORTANT: a position may still be open on your account after this !!!")
print("!!! Check your cTrader app and close it manually if needed.            !!!")