import asyncio
import hashlib
import json
import math
import obs
import os
import re
from io import BytesIO
from pathlib import Path
from urllib.request import Request as HttpRequest, urlopen

from m1 import (DOMAINS, expanded_tokens, keyword_scores, require_keys, require_text,
                secondary_confirmed, smalltalk, SMALLTALK_MESSAGES, tokens, unique_object)
from orchestrator import Service, ServiceError

DEFAULT_CORPUS = Path(__file__).resolve().parent / "data" / "corpus.json"

# A query is vague when it carries little topical content AND reads like a follow-up or
# hedge — not merely when it is short ('joke' is short but clearly off-domain).
VAGUE_WORDS = {"still", "again", "same", "before", "something", "anything", "someone", "help",
               "issue", "problem", "thing", "stuff", "earlier", "previous", "broken"}
VAGUE_RE = re.compile(r"\b(" + "|".join(VAGUE_WORDS) + r")\b", re.I)


def is_vague(query):
    """A follow-up/hedge with under two topical tokens is vague; a short query with a real
    topic ('joke', 'weather') is off-domain, not vague."""
    topical = tokens(query) - VAGUE_WORDS
    return len(topical) < 2 and bool(VAGUE_RE.search(query))


DOCUMENT_EXTENSIONS = (".md", ".txt", ".pdf", ".docx")


def extract_text(filename, content):
    """Bytes → text for corpus files. pypdf/python-docx are imported lazily so the
    keyword/embedding path keeps working if they are not installed."""
    ext = Path(filename).suffix.lower()
    if ext in (".md", ".txt"):
        return content.decode("utf-8")
    if ext == ".pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(content)).pages)
    if ext == ".docx":
        from docx import Document
        return "\n".join(paragraph.text for paragraph in Document(BytesIO(content)).paragraphs)
    raise ValueError(f"Unsupported file type: {ext}")


def doc_title(text, fallback):
    """A leading markdown heading becomes the document title; otherwise the filename does."""
    lines = text.splitlines()
    return lines[0].lstrip("#").strip() if lines and lines[0].startswith("#") else fallback


def load_documents(path=DEFAULT_CORPUS, docs_dir=None):
    """Load corpus.json and merge any real documents dropped into corpus.d/.

    corpus.d/<domain>/<name>.md or .txt files become extra sources for that domain —
    this is how the team adds real org docs without touching the seed file."""
    with Path(path).open(encoding="utf-8") as handle:
        value = json.load(handle, object_pairs_hook=unique_object)
    documents = value["documents"] if isinstance(value, dict) and "documents" in value else value
    if docs_dir is None:
        docs_dir = Path(path).resolve().parent / "corpus.d"
    return merge_doc_dir(documents, docs_dir)


def merge_doc_dir(documents, docs_dir):
    """Merge corpus.d/<domain>/*.md|txt files into corpus domains as sources.
    A leading markdown heading becomes the document title; otherwise the filename does."""
    root = Path(docs_dir)
    if not root.is_dir():
        return documents
    domains = documents.setdefault("domains", {})
    for domain_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        domain = domain_dir.name.casefold()
        if domain not in DOMAINS:
            raise ValueError(f"corpus.d folder '{domain_dir.name}' is not a routing domain: {sorted(DOMAINS)}")
        body = domains.setdefault(domain, {"title": domain.title(), "keywords": [], "sources": []})
        for file in sorted(p for p in domain_dir.iterdir() if p.suffix in DOCUMENT_EXTENSIONS):
            text = extract_text(file.name, file.read_bytes()).strip()
            if not text:
                continue
            title = doc_title(text, file.stem)
            content = "\n".join(text.splitlines()[1:]).strip() if text.startswith("#") else text
            doc_id = f"{domain}-{re.sub(r'[^a-z0-9]+', '-', file.stem.casefold()).strip('-')}"
            body["sources"].append({"id": doc_id, "title": title, "content": content or title,
                                    "format": file.suffix.lstrip("."), "file": str(file)})
    return documents


CHUNK_CHARS = 1200  # paragraphs are packed up to this; real docs get split, small docs stay whole


def chunk_text(text, max_chars=CHUNK_CHARS):
    """Split document content into retrieval chunks: paragraphs packed greedily up to
    max_chars, oversized paragraphs split on sentences, monster sentences hard-sliced
    with a small overlap so boundary context isn't lost."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return [text.strip()] if text.strip() else []
    pieces = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            pieces.append(paragraph)
            continue
        buf = ""
        for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
            if not buf:
                buf = sentence
            elif len(buf) + 1 + len(sentence) <= max_chars:
                buf += " " + sentence
            else:
                pieces.append(buf)
                buf = sentence
        if buf:
            pieces.append(buf)
    packed, buf = [], ""
    for piece in pieces:
        if not buf:
            buf = piece
        elif len(buf) + 2 + len(piece) <= max_chars:
            buf += "\n\n" + piece
        else:
            packed.append(buf)
            buf = piece
    if buf:
        packed.append(buf)
    chunks = []
    for piece in packed:
        while len(piece) > max_chars:
            chunks.append(piece[:max_chars])
            piece = piece[max_chars - 100:]
        chunks.append(piece)
    return chunks


class CorpusIndex:
    """Keyword/IDF domain corpus. Baseline retriever until the embedding index lands."""

    def __init__(self, documents):
        require_keys(documents, ("domains",), "corpus")
        if not isinstance(documents["domains"], dict) or not documents["domains"]:
            raise ValueError("Corpus must configure domains")
        self.documents = {}
        for domain, body in documents["domains"].items():
            if not isinstance(body, dict) or not {"title", "keywords", "sources"} <= set(body) \
               or not set(body) <= {"title", "keywords", "sources", "contact"}:
                raise ValueError("domain must define title, keywords and sources (contact optional)")
            if body.get("contact") is not None:
                require_text(body["contact"], "domain contact", 200)
            if domain not in DOMAINS:
                raise ValueError(f"Corpus domain is not a known routing domain: {domain}")
            require_text(body["title"], "domain title", 200)
            if not isinstance(body["keywords"], list) or not isinstance(body["sources"], list):
                raise ValueError("Invalid keywords or sources")
            sources = {}
            all_tokens = set()
            chunks = []
            for source in body["sources"]:
                require_keys(source, ("id", "title", "content"), "source",
                             optional=("format", "file"))
                require_text(source["id"], "source ID", 100)
                require_text(source["title"], "source title", 200)
                require_text(source["content"], "source content", 200000)
                require_text(source.get("format", "json"), "source format", 20)
                if source["id"] in sources:
                    raise ValueError("Duplicate source ID")
                source_tokens = tokens(f"{source['title']} {source['content']}")
                all_tokens |= source_tokens
                sources[source["id"]] = {"doc_id": source["id"], "title": source["title"],
                                        "content": source["content"], "tokens": source_tokens,
                                        "format": source.get("format", "json"),
                                        "file": source.get("file")}
                for index, piece in enumerate(chunk_text(source["content"])):
                    chunks.append({"doc_id": source["id"], "title": source["title"], "index": index,
                                   "text": piece, "tokens": tokens(f"{source['title']} {piece}")})
            self.documents[domain] = {"title": body["title"],
                                      "contact": body.get("contact"),
                                      "keywords": [k.casefold() for k in body["keywords"]],
                                      "sources": sources, "chunks": chunks, "all_tokens": all_tokens}
        self._calculate_idf()

    def _calculate_idf(self):
        counts = {}
        for domain in self.documents.values():
            for token in domain["all_tokens"]:
                counts[token] = counts.get(token, 0) + 1
        total = len(self.documents)
        self.idf = {token: math.log((1 + total) / (1 + count)) for token, count in counts.items()}

    def titles(self):
        return {domain: body["title"] for domain, body in self.documents.items()}

    def keywords(self):
        return {domain: list(body["keywords"]) for domain, body in self.documents.items()}

    def scores(self, query, domains=None):
        """Raw evidence scores per domain; used as the deterministic routing-confidence scorer."""
        query_tokens = expanded_tokens(query)
        result = {}
        for domain in (domains or self.documents):
            if domain not in self.documents:
                continue
            body = self.documents[domain]
            result[domain] = keyword_scores(query, {domain: body["keywords"]})[domain] * 3 + sum(
                self.idf.get(t, 1) for t in query_tokens & body["all_tokens"])
        return result

    def classify(self, query, available=None, margin=0.8, weak_evidence=4.0):
        """Deterministic keyword router. Offline fallback for the LLM router."""
        available = [d for d in (available or self.documents) if d in self.documents]
        kind = smalltalk(query)
        if kind and len(available) > 1:
            return {"action": "clarify", "tasks": [],
                    "options": [{"domain": d, "title": self.documents[d]["title"]} for d in available[:5]],
                    "message": SMALLTALK_MESSAGES[kind]}
        scores = self.scores(query, available)
        ordered = sorted(((s, d) for d, s in scores.items() if s > 0), reverse=True)
        if not ordered:
            if is_vague(query):
                return {"action": "clarify", "tasks": [],
                        "options": [{"domain": d, "title": self.documents[d]["title"]} for d in available[:3]],
                        "message": "Could you tell me a bit more about what you need?"}
            return {"action": "unsupported", "tasks": [], "options": [],
                    "message": "This does not match any configured knowledge area."}
        top = ordered[0][0]
        if len(ordered) > 1 and ordered[1][0] >= margin * top and top < weak_evidence:
            return {"action": "clarify", "tasks": [],
                    "options": [{"domain": d, "title": self.documents[d]["title"]} for _, d in ordered[:3]],
                    "message": "Which area should I check?"}
        query_tokens = expanded_tokens(query)
        strong = [(s, d) for s, d in ordered if s >= 0.45 * top
                  and (d == ordered[0][1] or secondary_confirmed(query_tokens, self.documents[d]["keywords"]))]
        if len(strong) > 1:
            return {"action": "route", "options": [], "message": "",
                    "tasks": [{"domain": d,
                               "instruction": f"Answer the {self.documents[d]['title']} part using only retrieved sources."}
                              for _, d in strong]}
        top_domain = ordered[0][1]
        return {"action": "route",
                "tasks": [{"domain": top_domain,
                           "instruction": f"Answer the {self.documents[top_domain]['title']} part using only retrieved sources."}],
                "options": [], "message": ""}

    def retrieve(self, domain, instruction, limit=3):
        if not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError("limit must be 1..10")
        if domain not in self.documents:
            raise ValueError("Unknown domain")
        with obs.stage("RETRIEVAL", domain=domain, engine="keyword", top_k=limit) as out:
            query_tokens = expanded_tokens(instruction)
            ranked = []
            for chunk in self.documents[domain]["chunks"]:
                overlap = query_tokens & chunk["tokens"]
                if overlap:
                    ranked.append((sum(self.idf.get(t, 1) for t in overlap), chunk["doc_id"], chunk["index"], chunk))
            ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
            hits = [{"doc_id": c["doc_id"], "title": c["title"], "chunk": c["text"],
                     "content": c["text"], "score": score} for score, _, _, c in ranked[:limit]]
            self._log_hits(out, hits)
            return hits

    def _log_hits(self, out, hits):
        """Shared retrieval stats: count + score spread; doc/chunk ids only when
        LOG_RETRIEVED_DOCS is on (metadata, never the text itself)."""
        scores = [h["score"] for h in hits]
        out.update(hits=len(hits), score_max=round(max(scores), 3) if scores else None,
                   score_min=round(min(scores), 3) if scores else None,
                   score_avg=round(sum(scores) / len(scores), 3) if scores else None)
        if obs.cfg()["retrieved_docs"]:
            out["docs"] = [h["doc_id"] for h in hits]

    def add_source(self, domain, doc_id, title, content, fmt="txt", file=None):
        """Runtime ingestion: add one document to a domain and re-index. Called by
        POST /corpus/upload — new doc is retrievable immediately after this returns."""
        if domain not in self.documents:
            raise ValueError("Unknown domain")
        require_text(doc_id, "source ID", 100)
        require_text(title, "source title", 200)
        require_text(content, "source content", 200000)
        if not isinstance(fmt, str) or fmt not in ("md", "txt", "pdf", "docx"):
            raise ValueError("Unknown source format")
        body = self.documents[domain]
        if doc_id in body["sources"]:
            raise ValueError("Duplicate source ID")
        source_tokens = tokens(f"{title} {content}")
        body["all_tokens"] |= source_tokens
        body["sources"][doc_id] = {"doc_id": doc_id, "title": title,
                                   "content": content, "tokens": source_tokens,
                                   "format": fmt, "file": file}
        for index, piece in enumerate(chunk_text(content)):
            body["chunks"].append({"doc_id": doc_id, "title": title, "index": index,
                                   "text": piece, "tokens": tokens(f"{title} {piece}")})
        self._calculate_idf()
        return doc_id

    def services(self, mock=False, answerer=None):
        return {domain: Service(domain, "local", self.handler(domain, answerer),
                                resource_group="corpus", mock=mock) for domain in self.documents}

    def handler(self, domain, answerer=None):
        async def call(payload, request_id):
            query = payload.get("request", {}).get("query", "")
            # retrieve may hit the synchronous embedder (HTTP) — keep it off the event loop
            chunks = await asyncio.to_thread(
                self.retrieve, domain, f"{query} {payload.get('instruction', '')}", 3)
            if not chunks:
                raise ServiceError("no_evidence")
            obs.event("CONTEXT_BUILT", domain=domain, chunks=len(chunks),
                      context_chars=sum(len(c["chunk"]) for c in chunks),
                      citations=len({c["doc_id"] for c in chunks}),
                      top_k=3, context_truncated=False)
            answer = self.summarize(f"{query} {payload.get('instruction', '')}", chunks)
            if answerer is not None:
                try:
                    answer = await answerer(query, [c["chunk"] for c in chunks])
                except Exception:
                    pass   # extractive answer stands — never let prose generation lose the evidence
            contact = self.documents[domain].get("contact")
            if contact:
                answer += f"\n\nIf that does not solve it: {contact}."
            seen, citations = set(), []
            for chunk in chunks:
                if chunk["doc_id"] not in seen:
                    seen.add(chunk["doc_id"])
                    citations.append({"doc_id": chunk["doc_id"], "title": chunk["title"]})
            return {"answer": answer, "citations": citations,
                    "evidence": [{"doc_id": c["doc_id"], "chunk": c["chunk"]} for c in chunks]}
        return call

    def summarize(self, instruction, evidence):
        """Extractive baseline: returns the most relevant source sentences, never new facts."""
        joined = " ".join(item["content"] for item in evidence)
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", joined) if s.strip()]
        query = expanded_tokens(instruction)
        scored = sorted(((len(tokens(s) & query), -i, s) for i, s in enumerate(sentences)), reverse=True)
        selected = [s for _, _, s in scored[:2] if s]
        return " ".join(selected) or sentences[0]


class OllamaEmbedder:
    """Text embeddings via the local Ollama embedding API (loopback only).
    Vectors persist to a small JSON cache keyed by sha256(model + text) so
    restarts skip re-embedding the whole corpus; a cache file is optional."""

    def __init__(self, model=None, url="http://127.0.0.1:11434/api/embed", timeout=None,
                 cache_path=None):
        self.model = model or os.environ.get("CORTEX_EMBED_MODEL", "qwen3-embedding:8b")
        timeout = timeout if timeout is not None \
            else float(os.environ.get("CORTEX_EMBED_TIMEOUT", "60"))
        self.url, self.timeout = url, timeout
        self._cache = None        # lazily warm-loaded from disk
        self._dirty = False
        self._cache_path = (DEFAULT_CORPUS.parent / "embed-cache.json") if cache_path is None \
            else (Path(cache_path) if cache_path else None)  # cache_path=False disables

    @staticmethod
    def _key(model, text):
        return hashlib.sha256(f"{model}\x00{text}".encode("utf-8")).hexdigest()

    def _store(self):
        if self._cache is None:
            self._cache = {}
            if self._cache_path:
                try:
                    saved = json.loads(self._cache_path.read_text(encoding="utf-8"))
                    for key, vector in saved.items():
                        if isinstance(vector, list):
                            self._cache[key] = vector
                except (OSError, ValueError):
                    pass  # corrupt/unreadable cache → start empty, never a startup dep
        return self._cache

    def _persist(self):
        if not (self._cache_path and self._dirty and self._cache is not None):
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache), encoding="utf-8")
            tmp.replace(self._cache_path)
            self._dirty = False
        except OSError:
            pass

    def _put(self, store, key, vector):
        if len(store) < 5000:
            store[key] = vector
            self._dirty = True

    def __call__(self, text):
        with obs.stage("EMBEDDING", model=self.model, input_chars=len(text)) as out:
            store = self._store()
            key = self._key(self.model, text)
            if key in store:
                out.update(cached=True, dims=len(store[key]))
                return store[key]
            payload = json.dumps({"model": self.model, "input": text, "keep_alive": "10m"}).encode("utf-8")
            call = HttpRequest(self.url, data=payload, headers={"Content-Type": "application/json"})
            with urlopen(call, timeout=self.timeout) as response:
                body = json.loads(response.read(5000001))
            vectors = body.get("embeddings")
            if not vectors or not all(isinstance(x, (int, float)) for x in vectors[0]):
                raise ValueError("Ollama returned no embedding")
            self._put(store, key, vectors[0])
            self._persist()
            out.update(cached=False, dims=len(vectors[0]))
            return vectors[0]

    def batch(self, texts):
        """Embed many texts in one /api/embed call — Ollama accepts a list input and
        returns aligned embeddings. Falls back to per-text calls if batching fails.
        NOTE: synchronous like __call__ — callers in async code must use to_thread."""
        store = self._store()
        keys = [self._key(self.model, t) for t in texts]
        missing = [t for t, k in zip(texts, keys) if k not in store]
        if missing:
            # Sub-batch, not one giant call: an 8b on CPU can outrun the socket
            # timeout on a large input list, and a mid-way failure would lose all
            # progress. Groups stay bounded and each one persists before the next.
            step = max(1, int(os.environ.get("CORTEX_EMBED_BATCH", "12")))
            for i in range(0, len(missing), step):
                group = missing[i:i + step]
                try:
                    payload = json.dumps({"model": self.model, "input": group,
                                          "keep_alive": "10m"}).encode("utf-8")
                    call = HttpRequest(self.url, data=payload, headers={"Content-Type": "application/json"})
                    with urlopen(call, timeout=self.timeout * 4) as response:
                        body = json.loads(response.read(20000001))
                    vectors = body.get("embeddings") or []
                    ok = len(vectors) == len(group) and all(
                        isinstance(v, list) and v and all(isinstance(x, (int, float)) for x in v)
                        for v in vectors)
                    if not ok:
                        raise ValueError("Ollama returned invalid batch embeddings")
                    for text, vector in zip(group, vectors):
                        self._put(store, self._key(self.model, text), vector)
                    self._persist()
                except Exception:
                    for text in group:  # graceful degradation to serial embedding
                        self(text)
        return [store[k] for k in keys]


def cosine(a, b):
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    return dot / (math.sqrt(na) * math.sqrt(nb)) if na and nb else 0.0


class SemanticIndex(CorpusIndex):
    """Embedding-backed corpus — drop-in for CorpusIndex with meaning-level matching.

    Every retrieval chunk is embedded once at init (docs are chunked by CorpusIndex; for
    a large corpus this becomes FAISS or Azure AI Search behind the same
    retrieve()/scores() contract). Keyword retrieval stays available via CorpusIndex
    for offline/no-deps runs.
    """

    ABSTAIN = 0.42     # below this similarity a domain has no usable evidence
    CLOSE_GAP = 0.05   # runner-ups within this of a weak top mean a genuine close race
    WEAK_TOP = 0.50    # top score below this -> weak evidence
    MULTI_FLOOR = 0.50 # domains must reach this absolute score to join a multi-topic route

    def __init__(self, documents, embedder=None):
        super().__init__(documents)
        self.embedder = embedder or OllamaEmbedder()
        entries = [(domain, chunk)
                   for domain, body in self.documents.items() for chunk in body["chunks"]]
        vectors = self._embed([f"{chunk['title']}. {chunk['text']}" for _, chunk in entries])
        self._vectors = [(domain, chunk, vector) for (domain, chunk), vector in zip(entries, vectors)]

    def _embed(self, texts):
        """Batch embed when the embedder supports it (one HTTP call); else serial."""
        batch = getattr(self.embedder, "batch", None)
        return batch(texts) if batch else [self.embedder(t) for t in texts]

    def add_source(self, domain, doc_id, title, content, fmt="txt", file=None):
        super().add_source(domain, doc_id, title, content, fmt=fmt, file=file)
        new_chunks = [c for c in self.documents[domain]["chunks"] if c["doc_id"] == doc_id]
        vectors = self._embed([f"{c['title']}. {c['text']}" for c in new_chunks])
        self._vectors.extend((domain, chunk, vector) for chunk, vector in zip(new_chunks, vectors))

    def scores(self, query, domains=None):
        """Hybrid domain evidence: cosine similarity plus a small keyword bonus — the keyword
        nudge keeps multi-topic queries from losing a domain to pure embedding noise."""
        qv = self.embedder(query)
        result = {d: 0.0 for d in (domains or self.documents)}
        for domain, _, vec in self._vectors:
            if domain in result:
                result[domain] = max(result[domain], cosine(qv, vec))

        keyword = keyword_scores(query, {d: self.documents[d]["keywords"] for d in result})
        return {d: result[d] + 0.03 * keyword.get(d, 0) for d in result}

    def classify(self, query, available=None, **kwargs):
        """Same decision contract as CorpusIndex.classify, driven by semantic similarity."""
        available = [d for d in (available or self.documents) if d in self.documents]
        kind = smalltalk(query)
        if kind and len(available) > 1:
            return {"action": "clarify", "tasks": [],
                    "options": [{"domain": d, "title": self.documents[d]["title"]} for d in available[:5]],
                    "message": SMALLTALK_MESSAGES[kind]}
        ordered = sorted(((s, d) for d, s in self.scores(query, available).items()), reverse=True)
        top = ordered[0][0] if ordered else 0.0
        if top < self.ABSTAIN:
            if is_vague(query):
                return {"action": "clarify", "tasks": [],
                        "options": [{"domain": d, "title": self.documents[d]["title"]} for d in available[:3]],
                        "message": "Could you tell me a bit more about what you need?"}
            return {"action": "unsupported", "tasks": [], "options": [],
                    "message": "This does not match any configured knowledge area."}
        close = [(s, d) for s, d in ordered[:3] if top - s <= self.CLOSE_GAP]
        if len(close) > 1 and top < self.WEAK_TOP:
            return {"action": "clarify", "tasks": [],
                    "options": [{"domain": d, "title": self.documents[d]["title"]} for _, d in close],
                    "message": "Which area should I check?"}
        # The top domain always routes once it clears ABSTAIN — MULTI_FLOOR only gates
        # secondary domains, and secondaries also need direct keyword evidence because
        # embedding similarity alone is too noisy at this corpus size.
        query_tokens = expanded_tokens(query)
        strong = [(s, d) for s, d in ordered
                  if d == ordered[0][1]
                  or (s >= self.MULTI_FLOOR and secondary_confirmed(query_tokens, self.documents[d]["keywords"]))]
        return {"action": "route", "options": [], "message": "",
                "tasks": [{"domain": d,
                           "instruction": f"Answer the {self.documents[d]['title']} part using only retrieved sources."}
                          for _, d in strong]}

    def retrieve(self, domain, instruction, limit=3):
        if not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError("limit must be 1..10")
        if domain not in self.documents:
            raise ValueError("Unknown domain")
        with obs.stage("RETRIEVAL", domain=domain, engine="semantic", top_k=limit) as out:
            qv = self.embedder(instruction)
            ranked = sorted(((cosine(qv, vec), chunk["doc_id"], chunk["index"], chunk)
                             for d, chunk, vec in self._vectors if d == domain),
                            key=lambda item: (-item[0], item[1], item[2]))
            hits = []
            for score, _, _, chunk in ranked:
                if score < self.ABSTAIN:
                    break
                hits.append({"doc_id": chunk["doc_id"], "title": chunk["title"], "chunk": chunk["text"],
                             "content": chunk["text"], "score": score})
                if len(hits) == limit:
                    break
            self._log_hits(out, hits)
            return hits


def create_seed_corpus(path=DEFAULT_CORPUS):
    data = {"metadata": {"version": "seed-v1", "review_status": "synthetic-seed-not-authoritative",
                         "human_review_required": True},
            "documents": {"domains": {
        "it": {"title": "IT help", "keywords": ["password", "laptop", "computer", "vpn", "wifi", "software", "network", "email", "account", "login"],
               "sources": [
                   {"id": "it-1", "title": "Password help", "content": "To reset a forgotten password, open the IT help portal and choose the password reset option. Reset links expire after 30 minutes."},
                   {"id": "it-2", "title": "WiFi and VPN", "content": "Campus WiFi uses your staff or student ID. VPN access requires the IT portal's remote access request form and manager approval."},
                   {"id": "it-3", "title": "Software requests", "content": "Licensed software is installed through the IT self-service portal. Requests are reviewed within two working days."}]},
        "hr": {"title": "HR", "keywords": ["leave", "holiday", "attendance", "payroll", "salary", "employee", "hiring", "hr"],
               "sources": [
                   {"id": "hr-1", "title": "Leave policy", "content": "Annual leave requests must be approved by the reporting manager before the planned leave date. Sick leave requires a certificate after three consecutive days."},
                   {"id": "hr-2", "title": "Payroll", "content": "Salary is credited on the last working day of each month. Payslips are available in the HR self-service portal."},
                   {"id": "hr-3", "title": "Attendance", "content": "Attendance corrections must be submitted to HR within five working days of the missed punch."}]},
        "fees": {"title": "Fees", "keywords": ["fee", "invoice", "payment", "scholarship", "receipt", "refund", "deadline", "dues"],
                 "sources": [
                     {"id": "fees-1", "title": "Fee payments", "content": "Students can download an itemized invoice and payment receipt from the fees portal after each transaction."},
                     {"id": "fees-2", "title": "Deadlines", "content": "Semester fees must be paid by the date on the academic calendar. Late payments attract a fine listed in the fee circular."},
                     {"id": "fees-3", "title": "Scholarships and refunds", "content": "Scholarship adjustments appear on the next invoice. Refund requests are processed through the fees portal within ten working days."}]},
        "facilities": {"title": "Facilities", "keywords": ["hostel", "room", "maintenance", "cleaning", "repair", "booking", "gym", "parking"],
                       "sources": [
                           {"id": "fac-1", "title": "Maintenance requests", "content": "A room maintenance request should include the room number, issue description and urgency level. Emergency issues go to the facilities hotline."},
                           {"id": "fac-2", "title": "Room bookings", "content": "Meeting rooms and halls are booked through the facilities portal at least two days in advance."},
                           {"id": "fac-3", "title": "Gym and recreation", "content": "The campus gym is open 6am to 9pm on working days. A valid ID card is required for entry."}]},
        "general": {"title": "General", "keywords": ["campus", "contact", "office", "timings", "location", "event"],
                    "sources": [
                        {"id": "gen-1", "title": "Campus contacts", "content": "The main help desk is in the admin block, open 9am to 5pm. Department contacts are listed in the staff directory."},
                        {"id": "gen-2", "title": "Academic calendar", "content": "Term dates, examination windows and holidays are published in the academic calendar on the portal."}]}}}}
    path = Path(path)
    if path.exists():
        raise FileExistsError("Refusing to overwrite an existing corpus")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def validate_corpus(path=DEFAULT_CORPUS):
    """Health check for the merged corpus (corpus.json + corpus.d drop-ins).
    Reports per-domain source/chunk counts and flags problems that would hurt
    retrieval — empty domains, tiny docs, near-duplicate IDs."""
    corpus = CorpusIndex(load_documents(path))  # structural errors raise here
    report = {"domains": {}, "issues": []}
    for domain, body in corpus.documents.items():
        report["domains"][domain] = {"title": body["title"], "sources": len(body["sources"]),
                                     "chunks": len(body["chunks"]),
                                     "keywords": len(body["keywords"])}
        if not body["sources"]:
            report["issues"].append(f"{domain}: no sources")
        if not body["keywords"]:
            report["issues"].append(f"{domain}: no routing keywords")
        for doc_id, source in body["sources"].items():
            if len(source["content"]) < 40:
                report["issues"].append(f"{domain}/{doc_id}: content under 40 chars")
            if len(source["tokens"]) < 5:
                report["issues"].append(f"{domain}/{doc_id}: fewer than 5 distinct tokens")
    return report


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Validate the merged Cortex corpus")
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS), help="path to corpus.json")
    args = parser.parse_args()
    report = validate_corpus(args.corpus)
    total_sources = sum(d["sources"] for d in report["domains"].values())
    total_chunks = sum(d["chunks"] for d in report["domains"].values())
    print(f"{total_sources} sources, {total_chunks} retrieval chunks")
    for domain, stats in report["domains"].items():
        print(f"  {domain:<10} {stats['sources']:>3} sources, {stats['chunks']:>3} chunks, "
              f"{stats['keywords']} keywords  —  {stats['title']}")
    for issue in report["issues"]:
        print(f"warning: {issue}")
    print("corpus OK" if not report["issues"] else f"{len(report['issues'])} issue(s) found")
    return 1 if report["issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
