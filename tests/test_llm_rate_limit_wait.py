import threading

from shared.llm import client as llm_client


def test_nonessential_llm_call_does_not_wait_for_a_rate_limit_token(monkeypatch):
    waits = []

    class ClosedBreaker:
        @staticmethod
        def is_open():
            return False

    class EmptyBucket:
        @staticmethod
        def acquire(timeout):
            waits.append(timeout)
            return False

    client = llm_client.LLMClient.__new__(llm_client.LLMClient)
    client._lock = threading.Lock()
    client._skipped_non_ess = 0
    monkeypatch.setattr(llm_client, "_LLM_MODE", "full")
    monkeypatch.setattr(llm_client, "_breaker", ClosedBreaker())
    monkeypatch.setattr(llm_client, "_bucket", EmptyBucket())
    monkeypatch.setattr(llm_client, "_cache_get", lambda key: None)
    monkeypatch.setattr(llm_client, "_NON_ESSENTIAL_RATE_LIMIT_WAIT_SEC", 0.0)
    monkeypatch.setattr(llm_client, "_ESSENTIAL_RATE_LIMIT_WAIT_SEC", 120.0)

    advisory = client.complete("system", "unique advisory prompt", essential=False)
    required = client.complete("system", "unique essential prompt", essential=True)

    assert not advisory.ok and advisory.reason == "rate_limiter_timeout"
    assert not required.ok and required.reason == "rate_limiter_timeout"
    assert waits == [0.0, 120.0]
