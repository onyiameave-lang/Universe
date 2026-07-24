import urllib.request, json, time

def post(path, body):
    t0 = time.time()
    req = urllib.request.Request(
        f'http://127.0.0.1:8000{path}',
        data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.loads(r.read())
    return data, round(time.time() - t0, 2)

# Test 1: ecosystem Hello
print("\n=== TEST 1: Hello ===")
data, elapsed = post('/ecosystem/chat', {'message': 'Hello, what can you do?'})
print(f'[{elapsed}s] Status: {data.get("status")} | Conf: {data.get("confidence")}')
print(f'Response:\n{data.get("response", "")}')

# Test 2: ecosystem market outlook
print("\n=== TEST 2: Market outlook ===")
data, elapsed = post('/ecosystem/chat', {'message': 'What is the market outlook today?'})
print(f'[{elapsed}s] Status: {data.get("status")} | Conf: {data.get("confidence")}')
print(f'Response:\n{data.get("response", "")}')
