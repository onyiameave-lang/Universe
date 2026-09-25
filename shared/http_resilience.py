"""Small in-process HTTP cache and status-aware backoff for public feeds."""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Dict, Optional, Tuple


class ResilientHTTPClient:
    def __init__(self, name: str, cache_ttl_sec: float = 300.0,
                 rate_limit_cooldown_sec: float = 900.0,
                 user_agent: str = "UniverseAI/1.0"):
        self.name = name
        self.cache_ttl_sec = cache_ttl_sec
        self.rate_limit_cooldown_sec = rate_limit_cooldown_sec
        self.user_agent = user_agent
        self._lock = threading.RLock()
        self._cache: Dict[str, Tuple[float, str]] = {}
        self._host_cooldowns: Dict[str, Tuple[float, int]] = {}
        self._endpoint_cooldowns: Dict[str, Tuple[float, int]] = {}
        self._host_failures: Dict[str, int] = {}

    @staticmethod
    def _host(url: str) -> str:
        return urllib.parse.urlsplit(url).netloc.lower()

    @staticmethod
    def _endpoint(url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        return f"{parsed.scheme}://{parsed.netloc.lower()}{parsed.path}"

    @staticmethod
    def _cache_key(url: str, headers: Optional[Dict[str, str]]) -> str:
        payload = repr((url, sorted((headers or {}).items()))).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _retry_after(value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, retry_at.timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                return None

    def get_text(self, url: str, headers: Optional[Dict[str, str]] = None,
                 timeout: float = 12.0) -> Optional[str]:
        now = time.time()
        host = self._host(url)
        endpoint = self._endpoint(url)
        key = self._cache_key(url, headers)

        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                return cached[1]
            self._cache.pop(key, None)
            host_cooldown = self._host_cooldowns.get(host)
            endpoint_cooldown = self._endpoint_cooldowns.get(endpoint)
            until = max(host_cooldown[0] if host_cooldown else 0.0,
                        endpoint_cooldown[0] if endpoint_cooldown else 0.0)
            if until > now:
                return None

        request = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent, **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode(
                    response.headers.get_content_charset() or "utf-8", errors="replace")
            with self._lock:
                self._host_failures.pop(host, None)
                completed_at = time.time()
                host_cooldown = self._host_cooldowns.get(host)
                endpoint_cooldown = self._endpoint_cooldowns.get(endpoint)
                if host_cooldown and host_cooldown[0] <= completed_at:
                    self._host_cooldowns.pop(host, None)
                if endpoint_cooldown and endpoint_cooldown[0] <= completed_at:
                    self._endpoint_cooldowns.pop(endpoint, None)
                self._cache[key] = (time.time() + self.cache_ttl_sec, body)
            return body
        except urllib.error.HTTPError as exc:
            retry_after = self._retry_after(exc.headers.get("Retry-After"))
            if exc.code == 429:
                delay = max(1.0, retry_after) if retry_after is not None else self.rate_limit_cooldown_sec
                cooldowns, cooldown_key = self._host_cooldowns, host
            elif exc.code in (401, 403):
                delay = 6 * 60 * 60
                cooldowns, cooldown_key = self._host_cooldowns, host
            elif exc.code == 404:
                delay = 6 * 60 * 60
                cooldowns, cooldown_key = self._endpoint_cooldowns, endpoint
            elif exc.code >= 500:
                with self._lock:
                    failures = self._host_failures.get(host, 0) + 1
                    self._host_failures[host] = failures
                delay = min(60.0 * (2 ** min(failures - 1, 5)), 1800.0)
                cooldowns, cooldown_key = self._host_cooldowns, host
            else:
                delay = 60.0
                cooldowns, cooldown_key = self._endpoint_cooldowns, endpoint
            with self._lock:
                cooldowns[cooldown_key] = (time.time() + delay, exc.code)
            logging.getLogger(self.name).warning(
                "HTTP %d from %s; suppressing retries for %.0fs",
                exc.code, endpoint, delay)
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            with self._lock:
                failures = self._host_failures.get(host, 0) + 1
                self._host_failures[host] = failures
                delay = min(30.0 * (2 ** min(failures - 1, 5)), 900.0)
                self._host_cooldowns[host] = (time.time() + delay, 0)
            logging.getLogger(self.name).warning(
                "Request to %s failed (%s); suppressing retries for %.0fs",
                endpoint, type(exc).__name__, delay)
            return None