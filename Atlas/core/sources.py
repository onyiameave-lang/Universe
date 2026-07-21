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

# FIX-P2-01: Per-source minimum gap between calls (seconds).
# Prevents burst-firing the same API endpoint on repeated queries.
# (Book IV Ch IX: "Systems shall not overwhelm external dependencies.")
_SOURCE_MIN_GAP: Dict[str, float] = {
    "semantic_scholar": 3.0,   # public tier: ~1 req/s; 3s gap is safe
    "arxiv":            2.0,   # no official limit but polite crawling required
    "gdelt":            2.0,   # GDELT asks for reasonable request rates
    "pubmed":           1.0,   # NCBI: 3 req/s without key; 1s gap is safe
    "crossref":         1.0,   # Crossref polite pool: 1 req/s
    "hackernews":       0.5,   # Algolia: generous limits
    "wikipedia":        0.5,   # Wikipedia REST: generous limits
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

    # Check if source is in cooldown from a previous 429 exhaustion
    with _source_lock:
        backoff_until = _source_backoff_until.get(src_name, 0.0)
        if now < backoff_until:
            remaining = round(backoff_until - now, 1)
            log.info("atlas.sources: %s in cooldown for %.1fs more — skipping", src_name, remaining)
            return [], "rate_limited_cooldown"

        # Enforce minimum gap between calls to this source
        min_gap = _SOURCE_MIN_GAP.get(src_name, 0.0)
        last_call = _source_last_call.get(src_name, 0.0)
        wait = min_gap - (now - last_call)
        if wait > 0:
            time.sleep(wait)

        _source_last_call[src_name] = time.time()

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
        for t in titles:
            b = _http_get(self.REST + urllib.parse.quote(t))
            if not b:
                continue
            try:
                d = json.loads(b)
                if d.get("extract"):
                    out.append(Evidence("wikipedia", d.get("title", t), d["extract"],
                                      url=d.get("content_urls", {}).get("desktop", {}).get("page", ""),
                                      credibility=SOURCE_CREDIBILITY["wikipedia"]))
            except Exception:
                continue
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

    def __init__(self, chronicle_client=None):
        self.sources = {
            "wikipedia": WikipediaSource(), "arxiv": ArxivSource(),
            "semantic_scholar": SemanticScholarSource(), "pubmed": PubMedSource(),
            "hackernews": HackerNewsSource(), "gdelt": GDELTSource(),
        }
        self.web = WebSource()
        self.pdf = PDFSource()
        # FIX-P2-07: Chronicle client for Memory First principle
        self.chronicle = chronicle_client

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
            results = self.chronicle.search(query=query, domain=domain, limit=3,
                                            requester="atlas.sources")
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
               sources: Optional[List[str]] = None) -> Dict[str, Any]:
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
        """
        _gather_deadline = time.time() + 25.0  # FIX-SRC-V3-04: 25s wall clock
        limit = {"shallow": 2, "standard": 3, "deep": 6}.get(depth, 3)
        chosen = self.sources_for(domain, sources)
        evidence: List[Evidence] = []
        status: Dict[str, Any] = {}

        # FIX-P2-07: Memory First — check Chronicle before external calls
        chronicle_ev = self._recall_from_chronicle(query, domain)
        if chronicle_ev:
            evidence.extend(chronicle_ev)
            status["chronicle"] = {"gathered": len(chronicle_ev), "status": "ok"}

        def _run(src_name: str) -> Tuple[str, List[Evidence], str]:
            # FIX-SRC-V3-04: Check deadline before starting each source.
            if time.time() > _gather_deadline:
                return src_name, [], "skipped_deadline"
            src = self.sources.get(src_name)
            if not src or not getattr(src, "available", False):
                return src_name, [], "unavailable"
            # FIX-P2-06: _gather_safe wraps with rate limiting + backoff
            items, st = _gather_safe(src_name, src.gather, query, limit)
            return src_name, items, st

        # FIX-SRC-V3-04: Use as_completed() instead of pool.map() so we can
        # collect results as they arrive and stop when the deadline hits,
        # rather than blocking until the slowest source finishes.
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(chosen), 6)) as pool:
            futures = {pool.submit(_run, src_name): src_name for src_name in chosen}
            for fut in concurrent.futures.as_completed(futures,
                                                        timeout=max(0.1, _gather_deadline - time.time())):
                try:
                    src_name, items, st = fut.result()
                    status[src_name] = {"gathered": len(items), "status": st}
                    evidence.extend(items)
                except concurrent.futures.TimeoutError:
                    # Deadline hit — cancel remaining futures and return what we have
                    for f in futures:
                        f.cancel()
                    log.info("atlas.sources: gather() deadline hit — returning %d items from %d sources "
                             "(Book II Principle V Graceful Degradation)", len(evidence), len(status))
                    status["_deadline_hit"] = True
                    break
                except Exception as exc:
                    src_name = futures.get(fut, "unknown")
                    log.warning("atlas.sources: future for %s raised: %s", src_name, exc)

        if not evidence:
            status["_summary"] = "no evidence gathered (sources unreachable or no matches)"
        else:
            ok_sources = [k for k, v in status.items()
                         if not k.startswith("_") and v.get("status") == "ok"]
            status["_summary"] = f"ok ({len(ok_sources)} sources returned results)"

        return {"evidence": evidence, "source_status": status, "sources_used": chosen}

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
def _patched_init(self, chronicle_client=None):
    _orig_init(self, chronicle_client=chronicle_client)
    self.sources["crossref"] = CrossrefSource()
SourceRegistry.__init__ = _patched_init