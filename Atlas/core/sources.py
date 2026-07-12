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
"""
from __future__ import annotations

import concurrent.futures
import io
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    from shared.config import get_config
except ImportError:
    get_config = None

_USER_AGENT = "AtlasResearchAI/2.0 (AI Ecosystem; constitutional research desk)"
_TIMEOUT = 12

# Per-source credibility priors (Book II Part III Ch VI). Peer-reviewed > preprint
# > encyclopedia > practitioner forum > raw web. Refined by corroboration later.
SOURCE_CREDIBILITY = {
    "semantic_scholar": 0.90, "pubmed": 0.90, "crossref": 0.88, "arxiv": 0.82,
    "wikipedia": 0.75, "gdelt": 0.65, "hackernews": 0.55, "web": 0.50,
    "pdf": 0.60, "unknown": 0.40,
}


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


def _http_get(url: str, headers: Optional[Dict[str, str]] = None, raw: bool = False):
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = resp.read()
            if raw:
                return data
            charset = resp.headers.get_content_charset() or "utf-8"
            return data.decode(charset, errors="replace")
    except Exception:
        return None


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'")):
        text = text.replace(a, b)
    text = re.sub(r"&#\d+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


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
    API = "http://export.arxiv.org/api/query"
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
        key = get_config().semantic_scholar_key if get_config else os.getenv("SEMANTIC_SCHOLAR_KEY", "").strip()
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
# Aggregating registry with parallel fetch
# ============================================================

class SourceRegistry:
    """All sources, fetched in parallel, with honest per-source status."""

    # domain -> which sources are most appropriate (Atlas reasons over these)
    DOMAIN_SOURCES = {
        "general": ["wikipedia", "semantic_scholar", "hackernews"],
        "research": ["semantic_scholar", "arxiv", "pubmed", "crossref"],
        "engineering": ["arxiv", "hackernews", "semantic_scholar"],
        "trading": ["gdelt", "hackernews", "wikipedia"],
        "prediction": ["gdelt", "hackernews", "wikipedia"],
        "news": ["gdelt", "hackernews"],
        "social": ["hackernews", "gdelt"],
        "medicine": ["pubmed", "semantic_scholar"],
        "science": ["arxiv", "semantic_scholar", "pubmed"],
    }

    def __init__(self):
        self.sources = {
            "wikipedia": WikipediaSource(), "arxiv": ArxivSource(),
            "semantic_scholar": SemanticScholarSource(), "pubmed": PubMedSource(),
            "hackernews": HackerNewsSource(), "gdelt": GDELTSource(),
        }
        self.web = WebSource()
        self.pdf = PDFSource()

    def sources_for(self, domain: str, override: Optional[List[str]] = None) -> List[str]:
        if override:
            return [s for s in override if s in self.sources]
        return self.DOMAIN_SOURCES.get(domain, self.DOMAIN_SOURCES["general"])

    def gather(self, query: str, domain: str = "general", depth: str = "standard",
               sources: Optional[List[str]] = None) -> Dict[str, Any]:
        limit = {"shallow": 2, "standard": 3, "deep": 6}.get(depth, 3)
        chosen = self.sources_for(domain, sources)
        evidence: List[Evidence] = []
        status: Dict[str, Any] = {}

        def _run(src_name):
            src = self.sources.get(src_name)
            if not src or not getattr(src, "available", False):
                return src_name, [], "unavailable"
            try:
                items = src.gather(query, limit=limit)
                return src_name, items, "ok" if items else "no_results"
            except Exception as exc:
                return src_name, [], f"error: {exc}"

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(chosen), 6)) as pool:
            for src_name, items, st in pool.map(_run, chosen):
                status[src_name] = {"gathered": len(items), "status": st}
                evidence.extend(items)

        if not evidence:
            status["_summary"] = "no evidence gathered (sources unreachable or no matches)"
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
            out.append(Evidence("crossref", title, it.get("abstract", title),
                              url=it.get("URL", ""), author=authors,
                              citations=it.get("is-referenced-by-count", 0) or 0,
                              date=str((it.get("published-print") or it.get("published-online") or {})
                                      .get("date-parts", [[""]])[0][0]),
                              credibility=SOURCE_CREDIBILITY["crossref"]))
        return out


# register crossref into any SourceRegistry instances created after import
_orig_init = SourceRegistry.__init__
def _patched_init(self):
    _orig_init(self)
    self.sources["crossref"] = CrossrefSource()
SourceRegistry.__init__ = _patched_init
