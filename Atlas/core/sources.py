"""
Atlas.core.sources
==================
Institutional-grade evidence acquisition. (Book II Ch IV Research Before
Assumption; Book I Part IV Article VII.)

A research desk does not rely on one encyclopedia. This layer aggregates many
real, mostly key-free public sources, each behind one `Source` contract, and
records provenance for every item so claims are traceable:

  * WikipediaSource   - encyclopedic grounding (REST + OpenSearch)
  * ArxivSource       - preprints / scientific frontier (Atom API)
  * SemanticScholar   - peer-reviewed papers + citation counts (Graph API, key optional)
  * PubMedSource      - biomedical literature (NCBI E-utilities, no key)
  * CrossrefSource    - DOIs, journals, citation metadata (no key)
  * HackerNewsSource  - practitioner/industry signal (Algolia API, no key)
  * GDELTSource       - global news coverage volume/tone (no key)
  * WebSource         - any public URL, HTML -> clean text
  * PDFSource         - extract text from a PDF URL (best-effort, stdlib-friendly)

Each source reports `available` and returns typed Evidence with a per-source
credibility prior. Failures degrade honestly (explicit reason, never fabricated).
Concurrency: sources are fetched in parallel with a thread pool for desk-speed.

FIX LOG (Phase 2):
  FIX-P2-01: Added per-source minimum call gaps and token-bucket rate limiting.
              (Book IV resilience; Book II No Silent Failures.)
  FIX-P2-02: Added full-jitter exponential backoff on HTTP 429 responses.
              (Book IV Ch IX Constitutional Deployment and Operations Standards.)
  FIX-P2-03: ArxivSource.API changed from http:// to https:// — plain HTTP
              fails DNS on VPN/restricted networks. (Book II Research Before
              Assumption.)
  FIX-P2-04: _http_get() now raises RateLimitError on 429 and ForbiddenError
              on 403 instead of returning None silently. (Book II No Silent
              Failures; Book IV Fail Loudly.)
  FIX-P2-05: DNS failures (socket.gaierror) are caught and returned as
              status="dns_error" — source is skipped, others continue.
              (Book IV resilience; Book II graceful degradation.)
  FIX-P2-06: SourceRegistry.gather() uses _gather_safe() that wraps every
              source call with backoff, per-source cooldown, and honest status
              reporting. (Book II Principle III Everything Communicates.)
  FIX-P2-07: Chronicle integration added to SourceRegistry — past evidence
              is retrieved before external calls (Memory First, Book II Principle I).
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import io
import json
import logging
import os
import random
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("atlas.sources")

from core.chronicle_access import bounded_chronicle_search  # type: ignore

_USER_AGENT = "AtlasResearchAI/2.0 (AI Ecosystem; constitutional research desk)"
_TIMEOUT = 5   # FIX-SRC-V3-01: reduced from 12s -> 5s per-source HTTP timeout
               # 12s * 3 sources = 36s just for one gather round; 5s keeps us under budget.
               # (Book II Principle V Graceful Degradation -- fast fail, not slow hang.)


def _build_ssl_context() -> Optional["ssl.SSLContext"]:
    """Use certifi's CA bundle if available, so requests don't depend on
    the OS's certificate store (a common source of CERTIFICATE_VERIFY_FAILED
    on Windows machines with corporate AV/VPN TLS inspection or an outdated
    system cert store). Falls back to Python's default context if certifi
    isn't installed."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None

_SSL_CONTEXT = _build_ssl_context()

# Per-source credibility priors (Book II Part III Ch VI). Peer-reviewed > preprint
# > encyclopedia > practitioner forum > raw web. Refined by corroboration later.
SOURCE_CREDIBILITY = {
    "semantic_scholar": 0.90, "pubmed": 0.90, "crossref": 0.88, "arxiv": 0.82,
    "wikipedia": 0.75, "gdelt": 0.65, "hackernews": 0.55, "web": 0.50,
    "pdf": 0.60, "unknown": 0.40, "chronicle": 0.85,
}

_EXTERNAL_QUERY_MAX_CHARS = 240
_PLANNED_QUERY_MAX_CHARS = 180
_EXTERNAL_QUERY_DETAIL_MARKERS = re.compile(
    r"(?i)\b(?:recent decision evidence|aggregate decision diagnostics|"
    r"point-in-time seven-timeframe|use the decision context|"
    r"determine what the evidence supports|separate sourced evidence|"
    r"do not recommend|do not infer)\b"
)


def _compact_external_query(query: str) -> str:
    """Keep research instructions/data local; send providers a short search phrase."""
    text = " ".join(str(query or "").split())
    marker = _EXTERNAL_QUERY_DETAIL_MARKERS.search(text)
    if marker:
        text = text[:marker.start()].strip()
    if len(text) > _EXTERNAL_QUERY_MAX_CHARS:
        text = text[:_EXTERNAL_QUERY_MAX_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:")
    return text or "institutional research evidence"


# One planner worker for the process. A slow LLM call can time out at the caller,
# but it cannot be killed safely; retaining the single worker prevents timed-out
# planner calls from accumulating threads or delaying source retrieval.
_query_planner_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="atlas-query-planner"
)
_query_planner_lock = threading.Lock()
_query_planner_future = None
_QUERY_FEEDBACK_KEY = "atlas.external_query_feedback.v1"


def _short_source_query(value: Any, fallback: str) -> str:
    text = " ".join(str(value or "").split()).strip(" \"'`.,;:")
    if len(text) > _PLANNED_QUERY_MAX_CHARS:
        text = text[:_PLANNED_QUERY_MAX_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:")
    return text or fallback


def _llm_source_queries(llm, query: str, sources: List[str], feedback: Dict[str, Any]):
    """Ask the configured LLM for concise retrieval phrases; never block callers."""
    global _query_planner_future
    if llm is None or not getattr(llm, "has_any", False) or len(query) <= 180:
        return None, "not_needed_or_unavailable"
    # Don't send serialized diagnostics to the planner. Keep only the deterministic
    # task heading and bounded context; the full query remains with Atlas/Chronicle.
    seed = _compact_external_query(query)[:1000]
    prompt = (
        "Create concise external search phrases for the listed search providers. "
        "Preserve the subject and concrete research question. Remove instructions, "
        "logs, serialized data, and trading decisions. Do not answer the question. "
        "Return JSON exactly as {\"queries\": {\"source_name\": \"phrase\"}}. "
        "Every phrase must be at most 180 characters. Use only listed source names.\n\n"
        f"Source names: {', '.join(sources)}\n"
        f"Prior retrieval yield for this same query: {json.dumps(feedback, ensure_ascii=True)[:800]}\n"
        f"Research heading/context: {seed}"
    )

    def call():
        return llm.complete_json(
            "You are Atlas's search query planner. Produce retrieval phrases, not conclusions.",
            prompt, temperature=0.0, max_tokens=180, essential=False,
        )

    with _query_planner_lock:
        if _query_planner_future is not None and not _query_planner_future.done():
            return None, "planner_busy_fallback"
        _query_planner_future = _query_planner_pool.submit(call)
        future = _query_planner_future
    try:
        parsed, result = future.result(timeout=2.0)
    except concurrent.futures.TimeoutError:
        return None, "planner_timeout_fallback"
    except Exception as exc:
        log.info("atlas.sources: query planner failed; using deterministic compaction (%s)",
                 type(exc).__name__)
        return None, "planner_error_fallback"
    if not getattr(result, "ok", False) or not isinstance(parsed, dict):
        return None, "planner_invalid_fallback"
    raw_queries = parsed.get("queries")
    if not isinstance(raw_queries, dict):
        return None, "planner_invalid_fallback"
    planned = {}
    for source in sources:
        candidate = raw_queries.get(source)
        if isinstance(candidate, str) and candidate.strip():
            planned[source] = _short_source_query(candidate, "")
    if not planned:
        return None, "planner_invalid_fallback"
    return planned, "llm_planned"

# FIX-P2-01: Per-source minimum gap between calls (seconds).
# Prevents burst-firing the same API endpoint on repeated queries.
# (Book IV Ch IX: "Systems shall not overwhelm external dependencies.")
_SOURCE_MIN_GAP: Dict[str, float] = {
    # FIX-SRC-V4-01: Reduced gaps for fast sources. These are minimum gaps between
    # calls to the SAME source, not between different sources. Since gather() uses
    # ThreadPoolExecutor with parallel workers, each source's gap is enforced per-thread,
    # not globally. The gaps below are conservative (safe for rate limits) but minimal
    # to avoid blocking the gather() deadline.
    "semantic_scholar": 1.0,   # public tier: ~1 req/s; 1s gap is safe (was 3.0)
    "arxiv":            0.5,   # no official limit but polite crawling required (was 2.0)
    "gdelt":            0.5,   # GDELT asks for reasonable request rates (was 2.0)
    "pubmed":           0.5,   # NCBI: 3 req/s without key; 0.5s gap is safe (was 1.0)
    "crossref":         0.5,   # Crossref polite pool: 1 req/s (was 1.0)
    "hackernews":       0.1,   # Algolia: generous limits (was 0.5)
    "wikipedia":        0.1,   # Wikipedia REST: generous limits (was 0.5)
}

# FIX-SRC-V4-02: Per-source timeout budget (total time allowed for gather() call).
# This is different from _TIMEOUT (per HTTP call). A source like Wikipedia makes
# multiple HTTP calls (search + summaries), so it needs a larger budget.
_SOURCE_TIMEOUT_BUDGET: Dict[str, float] = {
    "wikipedia":        8.0,   # search (1 call) + up to 3 summaries (3 calls) = 4 calls max
    "arxiv":            6.0,   # API call + parsing
    "semantic_scholar": 6.0,   # API call + parsing
    "pubmed":           5.0,   # API call + parsing
    "crossref":         5.0,   # API call + parsing
    "gdelt":            5.0,   # API call + parsing
    "hackernews":       5.0,   # API call + parsing
}

# FIX-P2-01: Per-source last-call timestamp and backoff-until timestamp.
_source_last_call: Dict[str, float] = {}
_source_backoff_until: Dict[str, float] = {}
_source_lock = threading.Lock()


# ============================================================
# Custom exceptions (FIX-P2-04)
# ============================================================

class RateLimitError(Exception):
    """Raised when a source returns HTTP 429 Too Many Requests."""

class ForbiddenError(Exception):
    """Raised when a source returns HTTP 403 Forbidden."""


# ============================================================
# HTTP helper (FIX-P2-04: raises on 429/403 instead of returning None)
# ============================================================

def _http_get(url: str, headers: Optional[Dict[str, str]] = None, raw: bool = False):
    """Fetch URL. Raises RateLimitError on 429, ForbiddenError on 403.
    Returns None on other errors (logged at WARNING). (FIX-P2-04)"""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT, context=_SSL_CONTEXT) as resp:
            data = resp.read()
            if raw:
                return data
            charset = resp.headers.get_content_charset() or "utf-8"
            return data.decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RateLimitError(f"HTTP 429 from {url}") from exc
        if exc.code == 403:
            raise ForbiddenError(f"HTTP 403 from {url}") from exc
        log.warning("HTTP %s fetch failed for %s: %s", exc.code, url, exc)
        return None
    except socket.gaierror:
        # FIX-P2-05: DNS failures are re-raised so _gather_safe() can tag them
        raise
    except Exception as exc:
        log.warning("HTTP fetch failed for %s: %s", url, exc)
        return None


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'")):
        text = text.replace(a, b)
    text = re.sub(r"&#\d+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ============================================================
# Rate-limiting wrapper (FIX-P2-01, FIX-P2-02)
# ============================================================

def _gather_safe(src_name: str, gather_fn, query: str, limit: int,
                 max_retries: int = 0) -> Tuple[List, str]:
    """
    Wraps a source's gather() call with:
      - Per-source minimum gap enforcement (FIX-P2-01)
      - Full-jitter exponential backoff on 429 (FIX-P2-02)
      - DNS error graceful skip (FIX-P2-05)
      - 403 graceful skip — no retries (FIX-P2-04)
      - Per-source 120s cooldown after exhausting retries (FIX-P2-06)

    FIX-SRC-V3-02: max_retries default changed from 2 -> 0.
    With max_retries=2, a single 429 caused 3 attempts with exponential backoff
    (1.4s + 3.1s + 6.6s) + 120s cooldown = 131s total, consuming the entire
    30s coordinator budget. With max_retries=0, a 429 fails immediately and
    the gather() moves on to the next source. Sources are plentiful; retrying
    a rate-limited source is not worth the time budget.
    (Book II Principle V Graceful Degradation -- skip fast, don't hang.)

    Returns (evidence_list, status_string).
    (Book II No Silent Failures — every outcome is named and logged.)
    """
    now = time.time()

    # FIX-SRC-V6-01 (2026-07-22): Move time.sleep() OUTSIDE the lock.
    # ROOT CAUSE: The original code called time.sleep(wait) while holding
    # _source_lock. Since gather() runs all sources in parallel via
    # ThreadPoolExecutor, every source thread had to acquire _source_lock
    # before starting. If wikipedia needed a 0.1s gap sleep, it held the lock
    # for 0.1s while ALL other source threads (arxiv, semantic_scholar, etc.)
    # queued up waiting. This turned parallel execution into sequential:
    #   Thread 1 (wikipedia): acquire lock, sleep 0.1s, release lock -> 0.1s
    #   Thread 2 (arxiv):     wait for lock, acquire, sleep 0.5s, release -> 0.6s total
    #   Thread 3 (semantic):  wait for lock, acquire, sleep 1.0s, release -> 1.6s total
    # Total: 1.6s of pure lock contention BEFORE any HTTP call starts.
    # FIX: Read the timestamps under the lock (fast), release the lock, THEN sleep.
    # This way threads only hold the lock for microseconds, not milliseconds.
    # Constitutional law: Book II Principle V Graceful Degradation -- parallel
    # execution must actually be parallel.

    # Step 1: Read state and compute wait under lock (fast, no I/O)
    with _source_lock:
        backoff_until = _source_backoff_until.get(src_name, 0.0)
        if now < backoff_until:
            remaining = round(backoff_until - now, 1)
            log.info("atlas.sources: %s in cooldown for %.1fs more — skipping", src_name, remaining)
            return [], "rate_limited_cooldown"

        min_gap = _SOURCE_MIN_GAP.get(src_name, 0.0)
        last_call = _source_last_call.get(src_name, 0.0)
        wait = min_gap - (now - last_call)
        # Update last_call timestamp NOW (before sleep) so other threads
        # see this source as "recently called" and don't pile up.
        _source_last_call[src_name] = time.time()

    # Step 2: Sleep OUTSIDE the lock so other source threads can proceed in parallel
    if wait > 0:
        time.sleep(wait)

    for attempt in range(max_retries + 1):
        try:
            items = gather_fn(query, limit=limit)
            return items, "ok" if items else "no_results"

        except RateLimitError as exc:
            # FIX-P2-02: Full-jitter exponential backoff
            base_wait = min(32.0, 2 ** attempt)
            jitter = random.uniform(0, base_wait)
            wait_sec = base_wait + jitter
            log.warning("atlas.sources: %s HTTP 429 (attempt %d/%d) — backing off %.1fs: %s",
                        src_name, attempt + 1, max_retries + 1, wait_sec, exc)
            if attempt < max_retries:
                time.sleep(wait_sec)
            else:
                # Exhausted retries — put source in 120s cooldown
                with _source_lock:
                    _source_backoff_until[src_name] = time.time() + 120.0
                log.error("atlas.sources: %s exhausted %d retries on 429 — "
                          "cooling down 120s. (Book II No Silent Failures)",
                          src_name, max_retries + 1)
                return [], "rate_limited"

        except ForbiddenError as exc:
            # FIX-P2-04: 403 = permanent for this call; no retries
            log.warning("atlas.sources: %s HTTP 403 — skipping (no retries): %s", src_name, exc)
            return [], "http_403"

        except socket.gaierror as exc:
            # FIX-P2-05: DNS failure — skip source, don't crash the desk
            log.warning("atlas.sources: %s DNS resolution failed — skipping: %s", src_name, exc)
            return [], "dns_error"

        except Exception as exc:
            log.warning("atlas.sources: %s unexpected error (attempt %d/%d): %s",
                        src_name, attempt + 1, max_retries + 1, exc)
            if attempt < max_retries:
                time.sleep(1.0)
            else:
                return [], f"error: {exc}"

    return [], "error: max_retries_exceeded"


# ============================================================
# Evidence dataclass
# ============================================================

@dataclass
class Evidence:
    source: str
    title: str
    text: str
    url: str = ""
    author: str = ""
    date: str = ""
    citations: int = 0                 # citation count where known (impact signal)
    relevance: float = 0.0
    credibility: float = 0.0
    corroboration: int = 0             # how many other sources agree (filled later)

    def to_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "title": self.title, "text": self.text[:700],
                "url": self.url, "author": self.author, "date": self.date,
                "citations": self.citations, "relevance": round(self.relevance, 3),
                "credibility": round(self.credibility, 3), "corroboration": self.corroboration}


# ============================================================
# Source adapters
# ============================================================

class WikipediaSource:
    name = "wikipedia"
    REST = "https://en.wikipedia.org/api/rest_v1/page/summary/"
    SEARCH = "https://en.wikipedia.org/w/api.php"
    available = True

    def gather(self, query: str, limit: int = 3) -> List[Evidence]:
        # FIX-SRC-V4-03: Fetch summaries in parallel instead of sequentially.
        # Old: search (1 call) + for each title: fetch summary (3 calls) = 4 calls sequential = 20s worst case
        # New: search (1 call) + fetch all summaries in parallel (3 calls concurrent) = ~5s worst case
        params = urllib.parse.urlencode({"action": "opensearch", "search": query,
                                        "limit": limit, "format": "json"})
        body = _http_get(f"{self.SEARCH}?{params}")
        titles = []
        if body:
            try:
                data = json.loads(body)
                titles = data[1] if len(data) > 1 else []
            except Exception:
                titles = []
        
        out = []
        if not titles:
            return out
        
        # Fetch all summaries in parallel
        def _fetch_summary(t: str) -> Optional[Evidence]:
            try:
                b = _http_get(self.REST + urllib.parse.quote(t))
                if not b:
                    return None
                d = json.loads(b)
                if d.get("extract"):
                    return Evidence("wikipedia", d.get("title", t), d["extract"],
                                  url=d.get("content_urls", {}).get("desktop", {}).get("page", ""),
                                  credibility=SOURCE_CREDIBILITY["wikipedia"])
            except Exception:
                pass
            return None
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(titles), 3)) as pool:
            futures = [pool.submit(_fetch_summary, t) for t in titles]
            for fut in concurrent.futures.as_completed(futures, timeout=5.0):
                try:
                    ev = fut.result()
                    if ev:
                        out.append(ev)
                except Exception:
                    pass
        
        return out


class ArxivSource:
    name = "arxiv"
    # FIX-P2-03: Changed from http:// to https:// — plain HTTP fails DNS on
    # VPN/restricted networks. (Book II Research Before Assumption.)
    API = "https://export.arxiv.org/api/query"
    available = True

    def gather(self, query: str, limit: int = 3) -> List[Evidence]:
        params = urllib.parse.urlencode({"search_query": f"all:{query}",
                                        "start": 0, "max_results": limit})
        body = _http_get(f"{self.API}?{params}")
        if not body:
            return []
        out = []
        for e in re.findall(r"(?s)<entry>(.*?)</entry>", body):
            title = self._tag(e, "title"); summary = self._tag(e, "summary")
            m = re.search(r"<id>(.*?)</id>", e); link = m.group(1).strip() if m else ""
            authors = re.findall(r"<name>(.*?)</name>", e)
            if title and summary:
                out.append(Evidence("arxiv", re.sub(r"\s+", " ", title).strip(),
                                  re.sub(r"\s+", " ", summary).strip(), url=link,
                                  author=", ".join(authors[:3]), date=self._tag(e, "published")[:10],
                                  credibility=SOURCE_CREDIBILITY["arxiv"]))
        return out

    @staticmethod
    def _tag(block, tag):
        m = re.search(rf"(?s)<{tag}[^>]*>(.*?)</{tag}>", block)
        return m.group(1).strip() if m else ""


class SemanticScholarSource:
    name = "semantic_scholar"
    API = "https://api.semanticscholar.org/graph/v1/paper/search"

    @property
    def available(self) -> bool:
        return True  # public endpoint; key optional raises rate limits

    def gather(self, query: str, limit: int = 3) -> List[Evidence]:
        params = urllib.parse.urlencode({"query": query, "limit": limit,
                                        "fields": "title,abstract,year,authors,citationCount,url"})
        headers = {}
        key = os.getenv("SEMANTIC_SCHOLAR_KEY", "").strip()
        if key:
            headers["x-api-key"] = key
        body = _http_get(f"{self.API}?{params}", headers=headers)
        if not body:
            return []
        try:
            data = json.loads(body)
        except Exception:
            return []
        out = []
        for p in data.get("data", []):
            abstract = p.get("abstract") or ""
            if not abstract:
                continue
            authors = ", ".join(a.get("name", "") for a in (p.get("authors") or [])[:3])
            out.append(Evidence("semantic_scholar", p.get("title", ""), abstract,
                              url=p.get("url", ""), author=authors,
                              date=str(p.get("year", "")), citations=p.get("citationCount", 0) or 0,
                              credibility=SOURCE_CREDIBILITY["semantic_scholar"]))
        return out


class PubMedSource:
    name = "pubmed"
    SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    SUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
    available = True

    def gather(self, query: str, limit: int = 3) -> List[Evidence]:
        params = urllib.parse.urlencode({"db": "pubmed", "term": query,
                                        "retmax": limit, "retmode": "json"})
        body = _http_get(f"{self.SEARCH}?{params}")
        if not body:
            return []
        try:
            ids = json.loads(body).get("esearchresult", {}).get("idlist", [])
        except Exception:
            return []
        if not ids:
            return []
        sparams = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(ids), "retmode": "json"})
        sbody = _http_get(f"{self.SUMMARY}?{sparams}")
        if not sbody:
            return []
        out = []
        try:
            result = json.loads(sbody).get("result", {})
            for pid in ids:
                item = result.get(pid, {})
                title = item.get("title", "")
                if title:
                    out.append(Evidence("pubmed", title, item.get("title", ""),
                                      url=f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
                                      date=item.get("pubdate", ""),
                                      credibility=SOURCE_CREDIBILITY["pubmed"]))
        except Exception:
            return out
        return out


class HackerNewsSource:
    name = "hackernews"
    API = "https://hn.algolia.com/api/v1/search"
    available = True

    def gather(self, query: str, limit: int = 3) -> List[Evidence]:
        params = urllib.parse.urlencode({"query": query, "tags": "story", "hitsPerPage": limit})
        body = _http_get(f"{self.API}?{params}")
        if not body:
            return []
        try:
            hits = json.loads(body).get("hits", [])
        except Exception:
            return []
        out = []
        for h in hits:
            title = h.get("title") or h.get("story_title") or ""
            if not title:
                continue
            out.append(Evidence("hackernews", title, h.get("story_text") or title,
                              url=h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                              date=h.get("created_at", "")[:10],
                              citations=h.get("points", 0) or 0,
                              credibility=SOURCE_CREDIBILITY["hackernews"]))
        return out


class GDELTSource:
    name = "gdelt"
    API = "https://api.gdeltproject.org/api/v2/doc/doc"
    available = True

    def gather(self, query: str, limit: int = 3) -> List[Evidence]:
        params = urllib.parse.urlencode({"query": query, "mode": "artlist",
                                        "maxrecords": limit, "format": "json"})
        body = _http_get(f"{self.API}?{params}")
        if not body:
            return []
        try:
            arts = json.loads(body).get("articles", [])
        except Exception:
            return []
        out = []
        for a in arts:
            title = a.get("title", "")
            if title:
                out.append(Evidence("gdelt", title, title, url=a.get("url", ""),
                                  date=a.get("seendate", "")[:8],
                                  credibility=SOURCE_CREDIBILITY["gdelt"]))
        return out


class WebSource:
    name = "web"
    available = True

    def fetch_url(self, url: str) -> Optional[Evidence]:
        body = _http_get(url)
        if not body:
            return None
        text = _strip_html(body)
        if len(text) < 40:
            return None
        m = re.search(r"(?is)<title>(.*?)</title>", body)
        title = _strip_html(m.group(1)) if m else url
        return Evidence("web", title, text[:3000], url=url, credibility=SOURCE_CREDIBILITY["web"])


class PDFSource:
    name = "pdf"
    available = True

    def fetch_pdf(self, url: str) -> Optional[Evidence]:
        data = _http_get(url, raw=True)
        if not data:
            return None
        text = self._extract(data)
        if not text or len(text) < 60:
            return None
        return Evidence("pdf", url.rsplit("/", 1)[-1], text[:4000], url=url,
                       credibility=SOURCE_CREDIBILITY["pdf"])

    def _extract(self, data: bytes) -> str:
        # Prefer a real PDF library if installed; else a minimal text scrape.
        try:
            from pypdf import PdfReader  # type: ignore
            reader = PdfReader(io.BytesIO(data))
            return " ".join((p.extract_text() or "") for p in reader.pages[:10])
        except Exception:
            pass
        # crude fallback: pull parenthesized text tokens from the raw stream
        try:
            raw = data.decode("latin-1", errors="ignore")
            chunks = re.findall(r"\(([^)]{3,})\)", raw)
            return _strip_html(" ".join(chunks))[:4000]
        except Exception:
            return ""


# ============================================================
# Aggregating registry with parallel fetch + rate limiting
# ============================================================

class SourceRegistry:
    """All sources, fetched in parallel, with honest per-source status.

    FIX-P2-06: gather() now uses _gather_safe() for every source call,
    providing rate limiting, backoff, and graceful error handling.
    FIX-P2-07: Chronicle integration — past evidence retrieved before
    external calls (Memory First, Book II Principle I).
    """

    # domain -> which sources are most appropriate (Atlas reasons over these)
    DOMAIN_SOURCES = {
        "general":     ["wikipedia", "hackernews", "semantic_scholar"],
        # FIX-SRC-V3-03: removed gdelt from general/engineering/science.
        # GDELT returns HTTP 429 constantly and has SSL timeouts. Wikipedia is
        # fast, free, and reliable -- it should be first for all general queries.
        # GDELT is kept only for news/trading/prediction where it's the right tool.
        "research":    ["semantic_scholar", "arxiv", "pubmed", "crossref"],
        "engineering": ["wikipedia", "arxiv", "hackernews", "semantic_scholar"],
        "trading":     ["gdelt", "hackernews", "wikipedia"],
        "prediction":  ["gdelt", "hackernews", "wikipedia"],
        "news":        ["gdelt", "hackernews"],
        "social":      ["hackernews", "gdelt"],
        "medicine":    ["pubmed", "semantic_scholar"],
        "science":     ["wikipedia", "arxiv", "semantic_scholar", "pubmed"],
    }

    def __init__(self, chronicle_client=None, llm=None):
        self.sources = {
            "wikipedia": WikipediaSource(), "arxiv": ArxivSource(),
            "semantic_scholar": SemanticScholarSource(), "pubmed": PubMedSource(),
            "hackernews": HackerNewsSource(), "gdelt": GDELTSource(),
        }
        self.web = WebSource()
        self.pdf = PDFSource()
        # FIX-P2-07: Chronicle client for Memory First principle
        self.chronicle = chronicle_client
        self.llm = llm

    def _query_feedback(self, fingerprint: str) -> Dict[str, Any]:
        """Read bounded, empirical retrieval-yield feedback from Chronicle state."""
        state = getattr(self.chronicle, "state", None)
        if state is None:
            return {}
        try:
            payload = state.get(_QUERY_FEEDBACK_KEY) or {}
            entry = (payload.get("queries") or {}).get(fingerprint, {})
            feedback = {}
            for name, stats in (entry.get("sources") or {}).items():
                attempts = int(stats.get("attempts", 0))
                successes = int(stats.get("success_runs", 0))
                if attempts:
                    feedback[name] = {
                        "runs": attempts,
                        "runs_with_results": successes,
                        "result_rate": round(successes / attempts, 3),
                        "last_successful_query": stats.get("last_successful_query", ""),
                    }
            return feedback
        except Exception as exc:
            log.info("atlas.sources: prior query feedback unavailable (%s)", type(exc).__name__)
            return {}

    def _record_query_outcomes(self, fingerprint: str, seed: str, queries: Dict[str, str],
                               source_status: Dict[str, Any]) -> None:
        """Store retrieval yield (not truth/quality) with revision-checked writes."""
        state = getattr(self.chronicle, "state", None)
        if state is None:
            return
        try:
            for _ in range(3):
                current = state.get_with_revision(_QUERY_FEEDBACK_KEY)
                payload = current.get("value") or {"schema": "atlas.external_query_feedback.v1",
                                                    "queries": {}}
                payload = dict(payload)
                entries = dict(payload.get("queries") or {})
                entry = dict(entries.get(fingerprint) or {"seed": seed[:240], "sources": {}})
                by_source = dict(entry.get("sources") or {})
                for name, planned_query in queries.items():
                    observed = source_status.get(name)
                    if not isinstance(observed, dict):
                        continue
                    stats = dict(by_source.get(name) or {
                        "attempts": 0, "success_runs": 0, "empty_runs": 0, "failed_runs": 0,
                    })
                    status = str(observed.get("status", "unknown"))
                    gathered = int(observed.get("gathered", 0) or 0)
                    stats["attempts"] = int(stats.get("attempts", 0)) + 1
                    if gathered > 0:
                        stats["success_runs"] = int(stats.get("success_runs", 0)) + 1
                        stats["last_successful_query"] = planned_query[:_PLANNED_QUERY_MAX_CHARS]
                    elif status == "no_results":
                        stats["empty_runs"] = int(stats.get("empty_runs", 0)) + 1
                    else:
                        stats["failed_runs"] = int(stats.get("failed_runs", 0)) + 1
                    stats["last_status"] = status[:40]
                    by_source[name] = stats
                entry["sources"] = by_source
                entries[fingerprint] = entry
                # Bound durable state. Keep most recently updated fingerprints.
                if len(entries) > 200:
                    entries = dict(list(entries.items())[-200:])
                payload.update({"schema": "atlas.external_query_feedback.v1",
                                "queries": entries})
                result = state.set(_QUERY_FEEDBACK_KEY, payload,
                                   owner="atlas.query_planner",
                                   expected_revision=int(current.get("revision", 0)))
                if result.get("status") == "complete":
                    return
            log.warning("atlas.sources: Chronicle query-feedback write conflicted repeatedly")
        except Exception as exc:
            log.warning("atlas.sources: could not persist query feedback: %s", exc)

    def sources_for(self, domain: str, override: Optional[List[str]] = None) -> List[str]:
        if override:
            return [s for s in override if s in self.sources]
        return self.DOMAIN_SOURCES.get(domain, self.DOMAIN_SOURCES["general"])

    def _recall_from_chronicle(self, query: str, domain: str) -> List[Evidence]:
        """FIX-P2-07: Retrieve past evidence from Chronicle before hitting external APIs.
        (Book II Principle I Memory First — ask Chronicle before generating new knowledge.)"""
        if self.chronicle is None:
            return []
        try:
            results = bounded_chronicle_search(
                self.chronicle, query=query, domain=domain, limit=3,
                requester="atlas.sources", timeout_sec=2.0,
            )
            if not isinstance(results, list):
                return []
            chronicle_evidence = []
            for mem in results:
                summary = mem.get("summary", "") or mem.get("content", "")
                if summary:
                    chronicle_evidence.append(Evidence(
                        source="chronicle",
                        title=f"[Memory] {summary[:80]}",
                        text=summary,
                        url="",
                        credibility=SOURCE_CREDIBILITY["chronicle"],
                    ))
            if chronicle_evidence:
                log.info("atlas.sources: Chronicle returned %d prior memories for '%s'",
                         len(chronicle_evidence), query[:60])
            return chronicle_evidence
        except Exception as exc:
            log.warning("atlas.sources: Chronicle recall failed (non-fatal): %s", exc)
            return []

    def gather(self, query: str, domain: str = "general", depth: str = "standard",
               sources: Optional[List[str]] = None,
               deadline_sec: float = 20.0) -> Dict[str, Any]:
        """
        Gather evidence from all chosen sources in parallel.
        FIX-P2-06: Uses _gather_safe() for every source — rate limiting,
        backoff, and graceful error handling built in.
        FIX-P2-07: Checks Chronicle first (Memory First principle).
        FIX-SRC-V3-04: Added 25s wall-clock deadline. gather() returns whatever
        evidence it has collected when the deadline hits, rather than waiting for
        all sources to complete. This prevents a slow source (GDELT SSL timeout,
        semantic_scholar 429) from consuming the entire coordinator budget.
        Individual source timeout is already 5s (_TIMEOUT), but the ThreadPoolExecutor
        pool.map() call blocks until ALL futures complete. The deadline ensures we
        return early with partial results rather than waiting for stragglers.
        (Book II Principle V Graceful Degradation -- partial results > no results.)
        
        FIX-SRC-V4-04: Added timing instrumentation to diagnose bottlenecks.
        """
        _gather_start = time.monotonic()
        try:
            budget = min(20.0, max(0.1, float(deadline_sec)))
        except (TypeError, ValueError):
            budget = 20.0
        _gather_deadline = _gather_start + budget
        limit = {"shallow": 2, "standard": 3, "deep": 6}.get(depth, 3)
        chosen = self.sources_for(domain, sources)
        source_query = _compact_external_query(query)
        fingerprint = hashlib.sha256(source_query.casefold().encode("utf-8")).hexdigest()[:24]
        prior_feedback = self._query_feedback(fingerprint)
        planned_queries, planner_status = _llm_source_queries(
            self.llm, query, chosen, prior_feedback
        )
        external_queries = {
            name: _short_source_query((planned_queries or {}).get(name), source_query)
            for name in chosen
        }
        if source_query != " ".join(str(query or "").split()) or planned_queries:
            log.info("atlas.sources: compacted external query from %d to %d characters",
                     len(str(query or "")), max((len(v) for v in external_queries.values()), default=0))
        evidence: List[Evidence] = []
        status: Dict[str, Any] = {}

        # FIX-P2-07: Memory First — check Chronicle before external calls
        chronicle_ev = self._recall_from_chronicle(query, domain)
        if chronicle_ev:
            evidence.extend(chronicle_ev)
            status["chronicle"] = {"gathered": len(chronicle_ev), "status": "ok"}
        
        # FIX-SRC-V4-04: Log Chronicle timing
        _chronicle_elapsed = time.monotonic() - _gather_start
        log.info("atlas.sources: gather() Chronicle recall took %.2fs", _chronicle_elapsed)

        def _run(src_name: str) -> Tuple[str, List[Evidence], str]:
            # FIX-SRC-V3-04: Check deadline before starting each source.
            if time.monotonic() >= _gather_deadline:
                return src_name, [], "skipped_deadline"
            src = self.sources.get(src_name)
            if not src or not getattr(src, "available", False):
                return src_name, [], "unavailable"
            # FIX-P2-06: _gather_safe wraps with rate limiting + backoff
            items, st = _gather_safe(src_name, src.gather, external_queries[src_name], limit)
            return src_name, items, st

        # FIX-SRC-V3-04: Use as_completed() instead of pool.map() so we can
        # collect results as they arrive and stop when the deadline hits,
        # rather than blocking until the slowest source finishes.
        # FIX-SRC-V6-02 (2026-07-22): Use a fixed 20s timeout for as_completed()
        # instead of computing it from the deadline AFTER Chronicle recall.
        # The old code computed timeout=max(0.1, _gather_deadline - time.time())
        # AFTER Chronicle recall. If Chronicle took 2s, the timeout was 23s.
        # But if Chronicle took 5s (slow), the timeout was 20s. This was fine.
        # The real problem: _gather_deadline was 25s from gather() START, but
        # the coordinator's budget for Atlas is now 28s. We need gather() to
        # complete in <=20s so research_engine.investigate() has 8s for synthesis.
        # Fixed 20s timeout is cleaner and more predictable than dynamic computation.
        _sources_timeout = max(0.0, _gather_deadline - time.monotonic())
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(len(chosen), 6)))
        futures = {}
        try:
            futures = {pool.submit(_run, src_name): src_name for src_name in chosen}
            for fut in concurrent.futures.as_completed(futures, timeout=_sources_timeout):
                try:
                    src_name, items, st = fut.result()
                    status[src_name] = {"gathered": len(items), "status": st}
                    evidence.extend(items)
                except Exception as exc:
                    src_name = futures.get(fut, "unknown")
                    log.warning("atlas.sources: future for %s raised: %s", src_name,
                                type(exc).__name__)
        except concurrent.futures.TimeoutError:
            status["_deadline_hit"] = True
            log.info("atlas.sources: gather() deadline hit — returning %d items from %d sources",
                     len(evidence), len(status))
        finally:
            for future in futures:
                future.cancel()
            # ThreadPoolExecutor's context manager waits for running HTTP calls;
            # that made a nominal timeout block until every slow source finished.
            pool.shutdown(wait=False, cancel_futures=True)

        if not evidence:
            status["_summary"] = "no evidence gathered (sources unreachable or no matches)"
        else:
            ok_sources = [k for k, v in status.items()
                         if not k.startswith("_") and v.get("status") == "ok"]
            status["_summary"] = f"ok ({len(ok_sources)} sources returned results)"

        # FIX-SRC-V4-04: Log total gather time
        _gather_elapsed = time.monotonic() - _gather_start
        log.info("atlas.sources: gather() complete in %.2fs (%d items from %d sources, deadline=%.1fs remaining)",
                 _gather_elapsed, len(evidence), len([s for s in status if not s.startswith("_")]),
                 max(0, _gather_deadline - time.monotonic()))

        self._record_query_outcomes(fingerprint, source_query, external_queries, status)
        query_plan = {
            "schema": "atlas.external_query_plan.v1",
            "query_fingerprint": fingerprint,
            "planner_status": planner_status,
            "queries": external_queries,
            "retrieval_feedback": {
                name: {"status": item.get("status"), "gathered": item.get("gathered", 0)}
                for name, item in status.items()
                if name in external_queries and isinstance(item, dict)
            },
        }
        return {"evidence": evidence, "source_status": status, "sources_used": chosen,
                "external_query_plan": query_plan}

    def fetch_url(self, url: str) -> Optional[Evidence]:
        if url.lower().endswith(".pdf"):
            return self.pdf.fetch_pdf(url)
        return self.web.fetch_url(url)


# Crossref is defined last so it can reuse helpers; registered lazily.
class CrossrefSource:
    name = "crossref"
    API = "https://api.crossref.org/works"
    available = True

    def gather(self, query: str, limit: int = 3) -> List[Evidence]:
        params = urllib.parse.urlencode({"query": query, "rows": limit})
        body = _http_get(f"{self.API}?{params}")
        if not body:
            return []
        try:
            items = json.loads(body).get("message", {}).get("items", [])
        except Exception:
            return []
        out = []
        for it in items:
            title = " ".join(it.get("title", []) or [])
            if not title:
                continue
            authors = ", ".join(f"{a.get('given','')} {a.get('family','')}".strip()
                              for a in (it.get("author") or [])[:3])
            raw_abstract = it.get("abstract", "") or ""
            abstract = self._clean_jats(raw_abstract) if raw_abstract else title
            out.append(Evidence("crossref", title, abstract or title,
                              url=it.get("URL", ""), author=authors,
                              citations=it.get("is-referenced-by-count", 0) or 0,
                              date=str((it.get("published-print") or it.get("published-online") or {})
                                      .get("date-parts", [[""]])[0][0]),
                              credibility=SOURCE_CREDIBILITY["crossref"]))
        return out

    @staticmethod
    def _clean_jats(raw: str) -> str:
        """Crossref abstracts come back as JATS XML, e.g.
        '<jats:title>Abstract</jats:title><jats:p>real text</jats:p>'.
        Strip all tags, drop a leading bare 'Abstract' label, and collapse
        whitespace, so the extracted text is clean prose, not markup."""
        text = re.sub(r"<[^>]+>", " ", raw)
        text = re.sub(r"\s+", " ", text).strip()
        if text.lower().startswith("abstract"):
            text = text[len("abstract"):].strip(" :.")
        return text


# register crossref into any SourceRegistry instances created after import
_orig_init = SourceRegistry.__init__
def _patched_init(self, chronicle_client=None, llm=None):
    _orig_init(self, chronicle_client=chronicle_client, llm=llm)
    self.sources["crossref"] = CrossrefSource()
SourceRegistry.__init__ = _patched_init
