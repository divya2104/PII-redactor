"""Client for the Azure AI Language PII endpoint, intended to run against a
locally hosted PII container (connected or disconnected) so that document
text never leaves the machine / private network."""
from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence
from urllib.parse import urlparse

import requests

# Service limits for synchronous PII requests (per document / per request).
MAX_CHARS_PER_DOC = 5000          # service limit is 5120; keep a margin
MAX_DOCS_PER_REQUEST = 5


class OfflineViolation(RuntimeError):
    """Raised when the configured endpoint is not on the local/private network."""


@dataclass(frozen=True)
class Entity:
    offset: int
    length: int
    category: str
    subcategory: Optional[str]
    confidence: float
    text: str

    @property
    def end(self) -> int:
        return self.offset + self.length


def _host_is_local(host: str) -> bool:
    if not host:
        return False
    if host == "localhost" or "." not in host:
        # "localhost" or a single-label name such as a docker-compose service ("pii")
        return True
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        return False
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not (ip.is_loopback or ip.is_private or ip.is_link_local):
            return False
    return True


def ensure_offline_endpoint(endpoint: str, allow_remote: bool = False) -> bool:
    """Returns True if the endpoint is remote (allowed only with allow_remote)."""
    host = urlparse(endpoint).hostname or ""
    if _host_is_local(host):
        return False
    if not allow_remote:
        raise OfflineViolation(
            f"Endpoint '{endpoint}' is not a local/private address. This tool is meant "
            "to run against the on-prem Azure Language PII container so that document "
            "text never leaves your network. Pass --allow-remote to override."
        )
    return True


class LanguagePiiClient:
    """Thin REST client for `/language/:analyze-text` (kind=PiiEntityRecognition)."""

    def __init__(
        self,
        endpoint: str = "http://localhost:5000",
        api_key: Optional[str] = None,
        api_version: str = "2022-05-01",
        language: str = "en",
        domain: Optional[str] = None,          # "phi" for health data
        categories: Optional[Sequence[str]] = None,
        model_version: str = "latest",
        timeout: float = 120.0,
        max_retries: int = 5,
        allow_remote: bool = False,
    ) -> None:
        self.remote = ensure_offline_endpoint(endpoint, allow_remote)
        self.url = endpoint.rstrip("/") + "/language/:analyze-text"
        self.api_version = api_version
        self.language = language
        self.domain = domain
        self.categories = list(categories) if categories else None
        self.model_version = model_version
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        # Local container: never route text through a proxy. Cloud: honour corporate proxies.
        self.session.trust_env = self.remote
        self.session.headers["Content-Type"] = "application/json"
        if api_key:
            self.session.headers["Ocp-Apim-Subscription-Key"] = api_key

    # -- low level -----------------------------------------------------------
    def _post(self, documents: List[dict]) -> dict:
        params = {"modelVersion": self.model_version, "stringIndexType": "UnicodeCodePoint"}
        if self.domain:
            params["domain"] = self.domain
        if self.categories:
            params["piiCategories"] = self.categories
        body = {
            "kind": "PiiEntityRecognition",
            "parameters": params,
            "analysisInput": {"documents": documents},
        }
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            try:
                r = self.session.post(
                    self.url, params={"api-version": self.api_version},
                    json=body, timeout=self.timeout,
                )
            except requests.ConnectionError:
                if attempt == self.max_retries:
                    raise
                time.sleep(delay); delay *= 2
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                time.sleep(float(r.headers.get("Retry-After", delay))); delay *= 2
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"PII service error {r.status_code}: {r.text[:500]}")
            return r.json()
        raise RuntimeError("unreachable")

    def health(self) -> tuple:
        """(ok, message). Container: GET /ready. Cloud: a one-word test request."""
        try:
            if not self.remote:
                base = self.url.rsplit("/language", 1)[0]
                r = self.session.get(base + "/ready", timeout=5)
                return r.ok, f"/ready returned {r.status_code}"
            self._post([{"id": "0", "language": self.language, "text": "hello"}])
            return True, "ok"
        except (requests.RequestException, RuntimeError) as exc:
            return False, str(exc)[:300]

    # -- public --------------------------------------------------------------
    def recognize(self, texts: Sequence[str]) -> List[List[Entity]]:
        """Detect PII in each text. Long texts are chunked transparently."""
        results: List[List[Entity]] = [[] for _ in texts]
        pieces = []  # (text_index, base_offset, chunk)
        for i, t in enumerate(texts):
            for off, chunk in chunk_text(t):
                if chunk.strip():
                    pieces.append((i, off, chunk))
        for batch in _batched(pieces, MAX_DOCS_PER_REQUEST):
            docs = [{"id": str(k), "language": self.language, "text": c}
                    for k, (_, _, c) in enumerate(batch)]
            data = self._post(docs)
            res = data.get("results", data)
            for err in res.get("errors", []):
                raise RuntimeError(f"PII document error: {err}")
            for d in res.get("documents", []):
                ti, base, chunk = batch[int(d["id"])]
                for e in d.get("entities", []):
                    off, ln = int(e["offset"]), int(e["length"])
                    results[ti].append(Entity(
                        offset=base + off, length=ln, category=e["category"],
                        subcategory=e.get("subcategory"),
                        confidence=float(e.get("confidenceScore", 1.0)),
                        text=chunk[off:off + ln],
                    ))
        return results


def chunk_text(text: str, max_chars: int = MAX_CHARS_PER_DOC):
    """Split text into <= max_chars pieces, preferring paragraph/line/word breaks."""
    start, n = 0, len(text)
    while start < n:
        end = min(start + max_chars, n)
        if end < n:
            window = text[start:end]
            for sep in ("\n\n", "\n", ". ", " "):
                cut = window.rfind(sep)
                if cut > max_chars // 2:
                    end = start + cut + len(sep)
                    break
        yield start, text[start:end]
        start = end


def _batched(items: list, n: int) -> Iterable[list]:
    for i in range(0, len(items), n):
        yield items[i:i + n]
