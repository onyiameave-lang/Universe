"""
Quick integration test — sends real messages to the running server and prints results.
Run with: python test_chat.py
"""
import urllib.request, urllib.error, json, time, sys

BASE = "http://127.0.0.1:8000"

def get(path, timeout=10):
    t0 = time.time()
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as r:
        data = json.loads(r.read())
    return data, round(time.time() - t0, 2)

def post(path, body, timeout=90):
    t0 = time.time()
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return data, round(time.time() - t0, 2)

def sep(title=""):
    print("\n" + "="*60)
    if title:
        print(f"  {title}")
        print("="*60)

TESTS = [
    ("Health check", "GET", "/health", None),
    ("Agents list", "GET", "/agents", None),
    ("Nexus status", "GET", "/agents/nexus/status", None),
    ("Oracle status", "GET", "/agents/oracle/status", None),
]

CHAT_TESTS = [
    ("ecosystem", "Hello, what can you do?"),
    ("ecosystem", "What is the market outlook today?"),
    ("ecosystem", "Give me a EURUSD signal"),
    ("nexus",     "Show me the ecosystem status"),
    ("oracle",    "What trading signals do you have?"),
    ("atlas",     "What is quantitative easing?"),
    ("sentinel",  "What is the latest market news?"),
    ("chronicle", "What do you remember about trading?"),
    ("aegis",     "What is the risk level right now?"),
]

errors = []

sep("GET ENDPOINTS")
for label, method, path, _ in TESTS:
    try:
        data, elapsed = get(path)
        if isinstance(data, list):
            print(f"  [{elapsed:5.2f}s] {label}: OK — list with {len(data)} items")
        elif isinstance(data, dict):
            print(f"  [{elapsed:5.2f}s] {label}: OK — keys={list(data.keys())[:4]}")
        else:
            print(f"  [{elapsed:5.2f}s] {label}: OK — {type(data).__name__}")
    except Exception as e:
        print(f"  [FAIL] {label}: {e}")
        errors.append(label)

sep("CHAT TESTS")
for target, message in CHAT_TESTS:
    if target == "ecosystem":
        path = "/ecosystem/chat"
        body = {"message": message}
    else:
        path = f"/agents/{target}/chat"
        body = {"message": message}
    try:
        t0 = time.time()
        data, elapsed = post(path, body, timeout=90)
        resp = data.get("response", data.get("message", ""))
        status = data.get("status", "?")
        conf = data.get("confidence")
        agent = data.get("agent", target)
        resp_preview = (resp[:120] + "...") if resp and len(resp) > 120 else resp
        print(f"\n  [{elapsed:5.2f}s] [{agent.upper()}] Q: {message[:50]}")
        print(f"           Status:{status} Conf:{conf}")
        print(f"           A: {resp_preview}")
    except urllib.error.URLError as e:
        print(f"\n  [FAIL] {target} — {message[:40]}: {e}")
        errors.append(f"{target}:{message[:30]}")
    except Exception as e:
        print(f"\n  [FAIL] {target} — {message[:40]}: {type(e).__name__}: {e}")
        errors.append(f"{target}:{message[:30]}")

sep("SUMMARY")
print(f"  Errors: {len(errors)}")
for e in errors:
    print(f"    - {e}")
if not errors:
    print("  All tests passed!")
