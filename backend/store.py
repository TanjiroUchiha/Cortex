import asyncio
import difflib
import hashlib
import json
import math
from backend import obs
import os
import re
from io import BytesIO
from pathlib import Path
from urllib.request import Request as HttpRequest, urlopen

from backend.access import can_view
from backend.corpus_package import parse_markdown
from models.m1 import (DOMAINS, DOMAIN_METADATA, PACKAGE_DOMAIN, expanded_tokens, keyword_scores, require_keys, require_text,
                secondary_confirmed, smalltalk, SMALLTALK_MESSAGES, STOPWORDS, token_variants, tokens,
                unique_object)
from backend.orchestrator import Service, ServiceError

DEFAULT_CORPUS = Path(__file__).resolve().parent.parent / "dataset" / "corpus.json"

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
    """Load corpus.json and merge every document under corpus.d/.

    corpus.d/<folder>/<name>.md|txt|pdf|docx becomes a source for that folder's
    routing domain. Folders are routing domains; PACKAGE_DOMAIN also accepts a
    legacy/extra category folder (library→general, finance→fees...) and folds
    it in, with metadata.original_category keeping provenance — this is how the
    team adds real org docs without touching the seed file."""
    with Path(path).open(encoding="utf-8") as handle:
        value = json.load(handle, object_pairs_hook=unique_object)
    documents = value["documents"] if isinstance(value, dict) and "documents" in value else value
    if docs_dir is None:
        docs_dir = Path(path).resolve().parent / "corpus.d"
    documents = merge_doc_dir(documents, docs_dir)
    for domain, body in documents["domains"].items():
        body["title"] = DOMAIN_METADATA[domain]["title"]
        body["keywords"] = list(DOMAIN_METADATA[domain]["keywords"])
    return documents


def merge_doc_dir(documents, docs_dir):
    """Merge corpus.d/<folder>/* files into corpus domains as sources.
    <folder> is a routing domain or a package category mapped via PACKAGE_DOMAIN.
    A leading markdown heading becomes the document title; otherwise the filename does."""
    root = Path(docs_dir)
    if not root.is_dir():
        return documents
    domains = documents.setdefault("domains", {})
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        name = folder.name.casefold()
        domain = name if name in DOMAINS else PACKAGE_DOMAIN.get(name)
        if domain is None:
            raise ValueError(f"corpus.d folder '{folder.name}' is not a routing domain or known category: {sorted(DOMAINS)}")
        body = domains.setdefault(domain, {"title": domain.title(), "keywords": [], "sources": []})
        for file in sorted(p for p in folder.iterdir() if p.suffix in DOCUMENT_EXTENSIONS):
            text = extract_text(file.name, file.read_bytes()).strip()
            if not text:
                continue
            source = document_source(file.name, text, domain)
            source.update(file=str(file), relative_path=file.relative_to(root).as_posix())
            body["sources"].append(source)
    return documents


def document_source(filename, text, domain):
    metadata, prose = parse_markdown(text) if Path(filename).suffix.lower() == ".md" else ({}, text)
    declared = metadata.get("category", domain)
    if declared != domain and PACKAGE_DOMAIN.get(declared) != domain:
        raise ValueError("Frontmatter category does not match the selected domain")
    title = metadata.get("title") or doc_title(prose, Path(filename).stem)
    content = "\n".join(prose.splitlines()[1:]).strip() if prose.startswith("#") else prose
    doc_id = metadata.get("document_id") or f"{domain}-{re.sub(r'[^a-z0-9]+', '-', Path(filename).stem.casefold()).strip('-')}"
    return {"id": doc_id, "title": title, "content": content or title,
            "format": Path(filename).suffix.lower().lstrip("."), "metadata": metadata}


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
        global_ids = set()
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
                             optional=("format", "file", "metadata", "relative_path"))
                require_text(source["id"], "source ID", 100)
                require_text(source["title"], "source title", 200)
                require_text(source["content"], "source content", 200000)
                require_text(source.get("format", "json"), "source format", 20)
                if source["id"] in global_ids:
                    raise ValueError("Duplicate source ID")
                global_ids.add(source["id"])
                source_tokens = tokens(f"{source['title']} {source['content']}")
                all_tokens |= source_tokens
                sources[source["id"]] = {"doc_id": source["id"], "title": source["title"],
                                        "content": source["content"], "tokens": source_tokens,
                                        "format": source.get("format", "json"),
                                        "file": source.get("file"),
                                        "metadata": source.get("metadata", {}),
                                        "relative_path": source.get("relative_path")}
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
        self._direct_cache = {}  # vocab changed — cached typo corrections are stale

    def titles(self):
        return {domain: body["title"] for domain, body in self.documents.items()}

    # Typo tolerance: "scolarship" matches nothing literally, so every domain
    # ties on generic words and the router clarifies. When a query token has no
    # corpus-vocabulary hit (itself or a plural variant), map it to the closest
    # vocabulary terms. Only fires for OOV tokens — a real corpus word never
    # fuzzies, so "fees" can't drift to "frees"/"flee". Cache resets in
    # _calculate_idf, so add_source/re-indexing stays consistent.
    _NEG_PREFIXES = ("un", "non", "dis", "mis", "im", "il", "ir")

    # Query-side paraphrase bridge: a keyword index can't tell that "close"
    # is answered by a doc saying "open 8am-10pm" / "hours". Small, curated —
    # only concept pairs where a campus query word plausibly wants the other
    # side's documents. Applied only to corpus-vocabulary words (checked in
    # _aliases) so it can't resurrect dead vocabulary.
    _CONCEPTS = {
        "close": {"open", "opening", "hours", "timings"},
        "closing": {"open", "opening", "hours", "timings"},
        "closed": {"open", "opening", "hours", "timings"},
        "cost": {"fee", "fees", "charge", "charges", "amount", "price"},
        "price": {"fee", "fees", "charge", "charges", "amount", "cost"},
        "pay": {"fee", "fees", "payment", "invoice"},
        "deadline": {"due", "date", "last"},
        "broken": {"repair", "maintenance", "report"},
        "job": {"employment", "placement", "recruitment"},
        "holiday": {"leave", "vacation", "holidays"},
        # derivational families — docs write "apply separately" where queries
        # say "application"; nominal/verb forms are the same evidence
        "application": {"apply", "applying", "applies", "applied"},
        "applications": {"apply", "applying", "applies", "applied"},
        "apply": {"application", "applications"},
        "applying": {"application", "applications"},
        "eligibility": {"eligible"},
        "eligible": {"eligibility"},
        "enrolment": {"enrol", "enroll", "enrolling", "enrolled", "enrollment"},
        "enrollment": {"enrol", "enroll", "enrolling", "enrolled", "enrolment"},
        "admission": {"admit", "admitted", "admitting"},
        "allocation": {"allocate", "allocated", "allocating"},
        "registration": {"register", "registered", "registering"},
        "register": {"registration", "registered", "registering"},
        "confirm": {"confirmation", "confirmed", "confirming"},
        "confirmation": {"confirm", "confirmed"},
        "approval": {"approve", "approved", "approving"},
        "reimbursement": {"reimburse", "reimbursed", "claim"},
        "borrowing": {"borrow", "borrowed", "borrows"},
        "borrow": {"borrowing", "borrowed", "borrows"},
        "renewal": {"renew", "renewed", "renewing"},
        "renew": {"renewal", "renewals"},
        "payment": {"pay", "paid", "paying"},
        "employment": {"employ", "employed"},
        "residence": {"resident", "reside", "residential"},
        "resident": {"residence", "residential"},
        "subscription": {"subscribe", "subscribed"},
        "timetable": {"schedule", "scheduled", "departure", "departures"},
        "schedule": {"timetable"},
        "clear": {"clearance", "cleared"},
        "clearance": {"clear", "cleared", "clears"},
        "graduate": {"graduation", "graduating", "graduates", "graduated"},
        "graduating": {"graduation", "graduate", "graduates", "graduated"},
        "graduation": {"graduate", "graduating", "graduates", "graduated"},
        "accessible": {"accessibility"},
        "accessibility": {"accessible"},
        "shortage": {"short", "insufficient"},
        "deactivation": {"deactivate", "deactivated", "disable", "disabled"},
        "deactivate": {"deactivation", "deactivated", "disable"},
        "delivery": {"deliver", "delivered", "collection", "collect"},
        "options": {"option", "methods", "ways"},
        "arrange": {"arrangement", "book", "booking", "request"},
        "resigning": {"resign", "resignation", "exit", "departure", "relieving", "separation"},
        "resign": {"resigning", "resignation", "exit", "departure", "relieving"},
        "resignation": {"resign", "resigning", "exit", "departure", "relieving"},
        "departure": {"depart", "departing", "leaving", "exit", "resignation"},
        "leaving": {"leave", "departure", "exit", "departing"},
        "dues": {"due", "clearance", "outstanding", "arrears"},
    }

    def _direct_aliases(self, token):
        """Variants/fuzzy matches only — the literal vocabulary of the term."""
        cached = self._direct_cache.get(token)
        if cached is None:
            forms = token_variants(token)
            if len(token) > 5 and token.endswith("ing"):  # opening→open, closing→close, timing→time
                forms |= {token[:-3], token[:-3] + "e"}
            direct = forms & self.idf.keys()
            if direct:
                cached = direct
            elif len(token) > 3:
                cutoff = 0.78 if len(token) < 8 else 0.8
                cached = set(difflib.get_close_matches(token, list(self.idf), n=2, cutoff=cutoff))
                # never fuzzy a negated word to its positive form — "unpublished"
                # is the opposite of "published", not a typo of it (covers
                # inflections too: unpublished → publishes)
                for prefix in self._NEG_PREFIXES:
                    if token.startswith(prefix) and len(token) - len(prefix) > 3:
                        stripped = token[len(prefix):]
                        cached = {a for a in cached
                                  if difflib.SequenceMatcher(None, a, stripped).ratio() < 0.8}
                        break
            else:
                cached = set()
            self._direct_cache[token] = cached
        return cached

    def _aliases(self, token):
        return self._direct_aliases(token) | (self._CONCEPTS.get(token, set()) & self.idf.keys())

    _QUERY_WRAPPER_RE = re.compile(
        r"^\s*what\s+(?:does|did|do|is|are)\s+the\s+[\w'-]*[\w\s'-]{0,40}?"
        r"(?:say|says|require|requires|state|states|mandate|mandates|cover|covers|specify|specifies)"
        r"\s+(?:about|for|regarding|on|of)\s+", re.I)

    def _probe_score(self, query, hit):
        """Evidence strength for routing probes: chunk score weighted by how
        much of the query's content appears in the document *title*. A doc
        titled 'Identity document requirements' is a far better owner than one
        merely containing the words."""
        if not hit:
            return 0.0
        title_terms = {a for t in tokens(hit["title"]) for a in self._aliases(t)}
        qterms = self._query_terms(query)
        overlap = len(title_terms & qterms) / max(1, len(qterms))
        return hit["score"] * (1 + overlap)

    def _query_terms(self, query):
        """expanded_tokens mapped through corpus vocabulary, with typo correction.
        Interrogative wrappers ("what does the protected record say about…")
        are stripped first — they are question scaffolding, not content
        vocabulary, and would otherwise drag in unrelated record/security docs."""
        query = self._QUERY_WRAPPER_RE.sub("", query, count=1)
        return {alias for t in tokens(query) for alias in self._aliases(t)}

    @classmethod
    def _query_bigrams(cls, query):
        """Adjacent content-word pairs from the raw query, order preserved."""
        query = cls._QUERY_WRAPPER_RE.sub("", query, count=1)
        words = [w for w in re.findall(r"[a-z]{3,}", query.casefold()) if w not in STOPWORDS]
        return list(zip(words, words[1:]))

    def keywords(self):
        return {domain: list(body["keywords"]) for domain, body in self.documents.items()}

    def scores(self, query, domains=None):
        """Raw evidence scores per domain; used as the deterministic routing-confidence scorer."""
        weights = self._query_weights(query)
        # keyword bonus must see typo/variant forms too — "scolarship" is one
        # edit away from the fees keyword and only the alias family knows it
        q_forms = {a for t in tokens(self._QUERY_WRAPPER_RE.sub("", query, count=1)) for a in (self._direct_aliases(t) or {t})}
        result = {}
        for domain in (domains or self.documents):
            if domain not in self.documents:
                continue
            body = self.documents[domain]
            kw = sum(len(parts) for parts in (tokens(k) for k in body["keywords"])
                     if parts and parts <= q_forms)
            result[domain] = kw * 3 + sum(
                w for fam, w in weights if fam & body["all_tokens"])
        return result

    def classify(self, query, available=None, margin=0.8, weak_evidence=4.0, viewer="admin"):
        """Deterministic keyword router. Offline fallback for the LLM router."""
        available = [d for d in (available or self.documents) if d in self.documents]
        kind = smalltalk(query)
        if kind and len(available) > 1:
            return {"action": "clarify", "tasks": [],
                    "options": [{"domain": d, "title": self.documents[d]["title"]} for d in available[:len(DOMAINS)]],
                    "message": SMALLTALK_MESSAGES[kind]}
        scores = self.scores(query, available)
        ordered = sorted(((s, d) for d, s in scores.items() if s > 0), reverse=True)
        if not ordered:
            if is_vague(query):
                return {"action": "clarify", "tasks": [],
                        "options": [{"domain": d, "title": self.documents[d]["title"]} for d in available[:len(DOMAINS)]],
                        "message": "Could you tell me a bit more about what you need?"}
            return {"action": "unsupported", "tasks": [], "options": [],
                    "message": "This does not match any configured knowledge area."}
        top = ordered[0][0]
        # Let the documents vote: probe retrieval in each plausible domain once,
        # then route to every domain whose evidence is real. One well-matched
        # document confirms a topic better than keyword presence ("library
        # access rules" — library counts as a location word so keyword
        # confirmation alone can't admit it); a domain with no visible hits can
        # only answer no_evidence. Keyword ties, dead ends and dual-coverage all
        # resolve the same way.
        probes, domain_hits = {}, {}
        for _, d in ordered[:5]:
            # probe at the skill's own limit — a small probe set trips the
            # coverage floor on long multi-topic queries and hides real
            # evidence (resigning+access+expense in hr probed empty at 3)
            hits = self.retrieve(d, query, limit=8, viewer=viewer, query=query)
            domain_hits[d] = hits
            probes[d] = max((self._probe_score(query, h) for h in hits), default=0.0)
        # topic-bearing query terms: only these can mark a second subject —
        # ubiquitous vocabulary ("document", "request") appears everywhere
        qterms = self._query_terms(query)
        topic_terms = {a for fam, w in self._query_weights(query) if w > 0.2 for a in fam} & qterms

        def topical_title(d):
            # union across the domain's hit set — the on-topic document is not
            # always the top-scoring chunk
            out = set()
            for hit in domain_hits.get(d, ()):
                out |= ({a for t in tokens(hit["title"]) for a in self._aliases(t)}
                        & qterms & topic_terms)
            return out

        # leaders: the strongest-evidence domain plus any keyword-owner — a
        # domain whose own keyword literally appears in the query (typo-aware:
        # "scolarship" is the fees keyword) is on-topic by definition
        leader = max(probes, key=probes.get) if max(probes.values(), default=0.0) > 0 else None
        q_forms = {a for t in tokens(self._QUERY_WRAPPER_RE.sub("", query, count=1))
                   for a in (self._direct_aliases(t) or {t})}
        owners = {d for _, d in ordered[:3]
                  if probes.get(d, 0) >= 1.0
                  and any(parts <= q_forms
                          for k in self.documents[d]["keywords"]
                          for parts in (tokens(k),) if parts)}
        leaders = ({leader} if leader else set()) | owners
        evidenced, covered = [], set()
        for _, d in ordered:
            if d not in probes or len(evidenced) >= 4:
                continue
            title_t = topical_title(d)
            if probes[d] >= 1.0 and (d in leaders
                                     or title_t - covered          # a genuinely new subject
                                     or (title_t and probes[d] >= 1.5)):  # a second source on it
                evidenced.append(d)
                covered |= title_t
        if len(evidenced) > 1:
            return {"action": "route", "options": [], "message": "", "evidence_resolved": True,
                    "tasks": [{"domain": d,
                               "instruction": f"Answer the {self.documents[d]['title']} part using only retrieved sources."}
                              for d in evidenced]}
        if evidenced:
            winner = leader if leader in evidenced else evidenced[0]
            return {"action": "route", "options": [], "message": "", "evidence_resolved": True,
                    "tasks": [{"domain": winner,
                               "instruction": f"Answer the {self.documents[winner]['title']} part using only retrieved sources."}]}
        if len(ordered) > 1 and ordered[1][0] >= margin * top and top < weak_evidence:
            # genuinely undecidable: keyword tie and no document evidence either —
            # options reflect probe strength so the user picks between what
            # actually has content
            ranked = sorted(((p, d) for d, p in probes.items()), reverse=True)
            if not ranked or ranked[0][0] <= 0:
                ranked = ordered
            return {"action": "clarify", "tasks": [],
                    "options": [{"domain": d, "title": self.documents[d]["title"]} for _, d in ranked[:3]],
                    "message": "Which area should I check?"}
        winner = ordered[0][1]
        return {"action": "route", "options": [], "message": "",
                "tasks": [{"domain": winner,
                           "instruction": f"Answer the {self.documents[winner]['title']} part using only retrieved sources."}]}

    # Abstention floor: a result set must cover this share of the query's
    # informative weight (IDF mass of its content tokens) or there is no real
    # evidence — keeps off-corpus questions that merely share campus vocabulary
    # from surfacing plausible-looking but unrelated hits. Tuned on the
    # expanded-corpus eval set: blocks most negative probes while retaining
    # ~87% of positive questions (see test_corpus.py / evaluate_corpus.py).
    COVERAGE_FLOOR = 0.40

    def _query_weights(self, query):
        """Each content token's alias family weighted at the family's max IDF.
        Variant/typo surface forms are one term — a document mentioning the rare
        'hostels' and one dense with 'hostel' are equal evidence for 'hostel',
        so families score once at their best IDF instead of letting a rare
        variant outrank the term's real documents. Concept-bridge members
        (close→open, application→apply) count at half weight: they're recall
        bridges, not literal evidence, and undiscounted they let a ubiquitous
        word like 'application' make every domain tie."""
        weights = []
        query = self._QUERY_WRAPPER_RE.sub("", query, count=1)
        for t in tokens(query):
            direct = self._direct_aliases(t) or {t}
            concept = self._CONCEPTS.get(t, set()) & self.idf.keys()
            w = max((self.idf.get(a, 1.0) for a in direct), default=1.0)
            w = max(w, 0.5 * max((self.idf.get(a, 1.0) for a in concept), default=0.0))
            # ubiquitous words bottom out at a small floor — "student record
            # access" is all-IDF-0 vocabulary yet still the question's content;
            # zero-weighted terms made the domain score indistinguishable from
            # no evidence at all
            weights.append((direct | concept, max(w, 0.05)))
        return weights

    def coverage(self, query, hits):
        """Share of the query's IDF mass covered by the union of hit tokens."""
        mass = {}
        query = self._QUERY_WRAPPER_RE.sub("", query, count=1)
        for t in tokens(query):
            if len(t) <= 3:
                continue
            direct = self._direct_aliases(t) or {t}
            concept = self._CONCEPTS.get(t, set()) & self.idf.keys()
            w = max((self.idf.get(a, 1.0) for a in direct), default=1.0)
            mass[t] = max(0.05, w, 0.5 * max((self.idf.get(a, 1.0) for a in concept), default=0.0))
        total = sum(mass.values())
        if not total:
            return 0.0
        hit_tokens = set()
        for h in hits:
            # ranking indexes title+body tokens; coverage must measure the same
            # vocabulary or a doc titled "Bus timetable" can't cover "timetable"
            hit_tokens |= h.get("tokens") or tokens(h["chunk"])
        # a query token is covered when the hits contain it or any of its
        # corpus aliases — otherwise a typo'd term would never count as covered
        return sum(mass[t] for t in mass if (self._aliases(t) or {t}) & hit_tokens) / total

    def _visible(self, domain, chunk, viewer):
        """Access check for one chunk's owning document."""
        return can_view(self.documents[domain]["sources"][chunk["doc_id"]].get("metadata", {}), viewer)

    def retrieve(self, domain, instruction, limit=3, viewer="admin", query=None):
        if not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError("limit must be 1..10")
        if domain not in self.documents:
            raise ValueError("Unknown domain")
        instruction = query or instruction
        with obs.stage("RETRIEVAL", domain=domain, engine="keyword", top_k=limit) as out:
            weights = self._query_weights(instruction)
            bigrams = self._query_bigrams(instruction)
            ranked = []
            for chunk in self.documents[domain]["chunks"]:
                if not self.version_eligible(domain, chunk, instruction):
                    continue
                if not self._visible(domain, chunk, viewer):
                    continue
                matched = [(w, len(fam & chunk["tokens"]))
                           for fam, w in weights if fam & chunk["tokens"]]
                if matched:
                    # one weight per query term family — a doc using "hostels"
                    # and a doc about "hostel" get the same evidence weight;
                    # matching several family members adds a small bonus so a
                    # chunk dense in the concept outranks a passing mention
                    score = sum(w + 0.15 * (n - 1) for w, n in matched)
                    # proximity: adjacent query content words appearing adjacent in
                    # the chunk ("opening hours") outrank scattered matches — pure
                    # IDF can't separate them when a term sits in every domain
                    low = chunk["text"].casefold()
                    score += sum(0.35 for a, b in bigrams if f"{a} {b}" in low)
                    ranked.append((score, chunk["doc_id"], chunk["index"], chunk))
            ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
            hits = [{"doc_id": c["doc_id"], "title": c["title"], "chunk": c["text"],
                     "content": c["text"], "tokens": c["tokens"], "score": score}
                    for score, _, _, c in ranked[:limit]]
            # coverage is measured on the user's query alone — the boilerplate
            # task instruction would dilute the mass estimate
            if hits and self.coverage(query or instruction, hits) < self.COVERAGE_FLOOR:
                out.update(coverage="below_floor")
                hits = []
            self._log_hits(out, hits)
            return hits

    def version_eligible(self, domain, chunk, query):
        source = self.documents[domain]["sources"][chunk["doc_id"]]
        metadata = source.get("metadata", {})
        if metadata.get("status") != "superseded":
            return True
        years = set(re.findall(r"\b(?:19|20)\d{2}\b", query))
        return bool(years & set(re.findall(r"\b(?:19|20)\d{2}\b", source["title"])))

    def _log_hits(self, out, hits):
        """Shared retrieval stats: count + score spread; doc/chunk ids only when
        LOG_RETRIEVED_DOCS is on (metadata, never the text itself)."""
        scores = [h["score"] for h in hits]
        out.update(hits=len(hits), score_max=round(max(scores), 3) if scores else None,
                   score_min=round(min(scores), 3) if scores else None,
                   score_avg=round(sum(scores) / len(scores), 3) if scores else None)
        if obs.cfg()["retrieved_docs"]:
            out["docs"] = [h["doc_id"] for h in hits]

    def add_source(self, domain, doc_id, title, content, fmt="txt", file=None, metadata=None, relative_path=None):
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
        if any(doc_id in b["sources"] for b in self.documents.values()):
            raise ValueError("Duplicate source ID")
        source_tokens = tokens(f"{title} {content}")
        body["all_tokens"] |= source_tokens
        body["sources"][doc_id] = {"doc_id": doc_id, "title": title,
                                   "content": content, "tokens": source_tokens,
                                   "format": fmt, "file": file, "metadata": metadata or {},
                                   "relative_path": relative_path}
        for index, piece in enumerate(chunk_text(content)):
            body["chunks"].append({"doc_id": doc_id, "title": title, "index": index,
                                   "text": piece, "tokens": tokens(f"{title} {piece}")})
        self._calculate_idf()
        return doc_id

    def remove_source(self, domain, doc_id):
        """Inverse of add_source: drop one document and re-index. Called by
        DELETE /corpus/{doc_id} — the persisted corpus.d file is the API's job."""
        body = self.documents.get(domain)
        if body is None or doc_id not in body["sources"]:
            return False
        del body["sources"][doc_id]
        body["chunks"] = [c for c in body["chunks"] if c["doc_id"] != doc_id]
        body["all_tokens"] = set().union(*(s["tokens"] for s in body["sources"].values())) \
            if body["sources"] else set()
        self._calculate_idf()
        return True

    def services(self, mock=False, answerer=None):
        return {domain: Service(domain, "local", self.handler(domain, answerer),
                                resource_group="corpus", mock=mock) for domain in self.documents}

    def handler(self, domain, answerer=None):
        async def call(payload, request_id):
            query = payload.get("request", {}).get("query", "")
            # retrieve may hit the synchronous embedder (HTTP) — keep it off the event loop.
            # Rank on the user query alone: the boilerplate instruction ("Answer the HR &
            # Admissions part…") overlaps whole doc families (e.g. admissions docs under
            # the hr domain) and ties with — or drowns — the relevant chunks.
            viewer = payload.get("request", {}).get("viewer_role", "admin")
            chunks = await asyncio.to_thread(
                self.retrieve, domain, query or payload.get("instruction", ""), 8,
                viewer, query)
            if not chunks:
                raise ServiceError("no_evidence")
            obs.event("CONTEXT_BUILT", domain=domain, chunks=len(chunks),
                      context_chars=sum(len(c["chunk"]) for c in chunks),
                      citations=len({c["doc_id"] for c in chunks}),
                      top_k=8, context_truncated=False)
            answer = self.summarize(query, chunks)
            if answerer is not None:
                try:
                    generated = await answerer(query, [c["chunk"] for c in chunks])
                    if isinstance(generated, str) and generated.strip():
                        answer = generated.strip()
                except Exception:
                    pass   # extractive answer stands — never let prose generation lose the evidence
            if not answer:
                raise ServiceError("no_evidence")
            contact = self.documents[domain].get("contact")
            if contact:
                answer += f"\n\nIf that does not solve it: {contact}."
            seen, citations = set(), []
            for chunk in chunks:
                if chunk["doc_id"] not in seen:
                    seen.add(chunk["doc_id"])
                    citations.append({"doc_id": chunk["doc_id"], "title": chunk["title"]})
            evidence = [{"doc_id": c["doc_id"], "chunk": c["chunk"]} for c in chunks]
            if contact:
                contact_id = f"{domain}-service-contact"
                citations.append({"doc_id": contact_id, "title": f"{self.documents[domain]['title']} service contact"})
                evidence.append({"doc_id": contact_id, "chunk": contact})
            return {"answer": answer, "citations": citations, "evidence": evidence}
        return call

    _MD_INLINE = re.compile(r"[*_`~]|!\[[^\]]*\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)")
    # section labels seen in the corpus — not answer content
    _MD_BOILER = re.compile(r"^(summary|overview|purpose( and scope)?|scope|background|"
                            r"introduction|what should the reader know|applies to|"
                            r"related documents?|responsibilities|process and records|"
                            r"exceptions?( and escalation)?|steps?|rules?|actions?|"
                            r"examples?|audit trail|review|definitions|notes?)\.?:?$", re.I)

    def _clean_sentences(self, text):
        """Unwrap hard-wrapped paragraphs, drop Markdown scaffolding (headings,
        bold section labels, list markers) and split into plain sentences."""
        cleaned = []
        blocks, paragraph = [], []
        for line in [*text.splitlines(), ""]:
            raw = line.strip()
            plain = self._MD_INLINE.sub(lambda m: m.group(1) or "", raw)
            if not raw or raw.startswith("#") or self._MD_BOILER.fullmatch(plain):
                if paragraph:
                    blocks.append((" ".join(paragraph), False))
                    paragraph = []
                continue
            item = re.match(r"^(?:[-*+>]|\d+[.)])\s+(.+)", raw)
            if item:
                if paragraph:
                    blocks.append((" ".join(paragraph), False))
                    paragraph = []
                blocks.append((self._MD_INLINE.sub(lambda m: m.group(1) or "", item[1]), True))
            else:
                paragraph.append(plain)
        for body, is_item in blocks:
            for s in re.split(r"(?<=[.!?])\s+", body):
                s = s.strip()
                # fragments with no terminal punctuation and few words are
                # heading remnants, not sentences
                if len(s) > 3 and not self._MD_BOILER.match(s) \
                        and (is_item or s[-1] in ".!?" or len(s.split()) > 6):
                    cleaned.append(s)
        return cleaned

    # answer-type priors: a "when" question wants a sentence carrying time words,
    # "how much/cost" wants numbers, "where" wants a place/contact. A sentence that
    # merely shares a noun ("salary certificates" for "when is my salary") scores
    # below one that actually answers the question type.
    _WHEN_TOKENS = {"day", "date", "days", "month", "deadline", "before", "after",
                    "within", "hour", "hours", "time", "timing", "timings", "schedule",
                    "week", "weeks", "year", "semester", "credited", "daily", "open"}
    _COST_TOKENS = {"rs", "inr", "fee", "fees", "amount", "cost", "price", "charge",
                    "fine", "deposit", "per", "free"}
    _WHO_TOKENS = {"contact", "email", "phone", "ext", "office", "desk", "counter",
                   "helpdesk", "coordinator", "manager", "warden"}
    _QTYPE_PRIORS = (("when", _WHEN_TOKENS), ("time", _WHEN_TOKENS),
                     ("hours", _WHEN_TOKENS), ("timings", _WHEN_TOKENS),
                     ("open", _WHEN_TOKENS), ("opening", _WHEN_TOKENS),
                     ("close", _WHEN_TOKENS), ("closing", _WHEN_TOKENS),
                     ("deadline", _WHEN_TOKENS), ("due", _WHEN_TOKENS),
                     ("timetable", _WHEN_TOKENS), ("schedule", _WHEN_TOKENS),
                     ("scheduled", _WHEN_TOKENS), ("departure", _WHEN_TOKENS),
                     ("much", _COST_TOKENS), ("cost", _COST_TOKENS), ("price", _COST_TOKENS),
                     ("fee", _COST_TOKENS), ("who", _WHO_TOKENS), ("where", _WHO_TOKENS))

    # a concrete "when" answer: clock times, weekdays, months, relative periods
    _WHEN_ANSWER_RE = re.compile(
        r"\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|\d{1,2}:\d{2}"
        r"|\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b"
        r"|\b(?:january|february|march|april|june|july|august|september|october|november|december)\b"
        r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+\d{1,2}\b"
        r"|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b"
        r"|\b(?:last|first)\s+\w*\s*(?:day|week|month)\b"
        r"|\b(?:each|every)\s+(?:day|week|month|working day)\b", re.I)
    _CONTACT_RE = re.compile(r"[\w.+-]+@[\w-]+|ext\.?\s*\d+|\b\d{4,}\b", re.I)

    def _has_answer_signal(self, sentence, priors):
        """True when the sentence carries a *concrete* answer to the question
        type — a real time for 'when', a number for 'cost', a contact for
        'who/where' — not just a keyword echo ('timings are posted on the
        portal' must not beat 'open 8am to 10pm')."""
        st = tokens(sentence)
        # "ext 4500" / a year is a digit but not a price — strip phone/email/4-digit
        # years before calling a sentence a cost answer
        amountish = bool(re.search(r"\d", re.sub(r"(?:ext\.?|extension)\s*\d+|[\w.+-]+@[\w-]+|\b(?:19|20)\d{2}\b",
                                                 "", sentence, flags=re.I)))
        for p in priors:
            if p is self._COST_TOKENS and amountish:
                return True
            if p is self._WHEN_TOKENS and self._WHEN_ANSWER_RE.search(sentence):
                return True
            if p is self._WHO_TOKENS and self._CONTACT_RE.search(sentence):
                return True
        return False

    # Package boilerplate embeds the doc title verbatim ("a request related to
    # <title> is incomplete"), so it scores maximal token overlap while saying
    # nothing about the question — discount it instead of letting it win picks.
    _BOILERPLATE_RE = re.compile(
        r"request related to|keeps the original case reference|case reference|"
        r"this record applies|record applies to|for assistance,? (?:contact|reach)|"
        r"how to use this|read the full policy|applies to all|"
        r"verifies the record|responsible owner is|approved portal", re.I)

    def _sentence_score(self, sentence, query_tokens, priors):
        """IDF-weighted overlap + small bonus for prior-token presence."""
        st = tokens(sentence)
        overlap = st & query_tokens
        if not overlap:
            return 0.0
        if self._BOILERPLATE_RE.search(sentence):
            return 0.0
        score = sum(max(self.idf.get(t, 1.0), 0.1) for t in overlap)
        if any(st & p for p in priors):
            score += 0.6 * max(self.idf.get(t, 1.0) for t in query_tokens)
        return score

    def summarize(self, instruction, evidence):
        """Extractive baseline: returns the most relevant source sentences, never new facts."""
        # evidence is rank-ordered; a sentence from hit #1 is likelier on-topic
        # than one from hit #3 that happens to share title tokens. Early
        # position also matters — package docs open with the core answer in
        # the summary and end with escalation/audit boilerplate.
        sentences = [(0.85 ** rank, pos, s)
                     for rank, item in enumerate(evidence)
                     for pos, s in enumerate(self._clean_sentences(item["content"]))]
        query = self._query_terms(instruction)
        raw_words = set(re.findall(r"[a-z]{2,}", instruction.casefold()))
        priors = [p for w, p in self._QTYPE_PRIORS if w in raw_words]
        scored = sorted(((self._has_answer_signal(s, priors),
                          rank_w * self._sentence_score(s, query, priors) * (1.3 if pos <= 2 else 1.0),
                          -i, s)
                         for i, (rank_w, pos, s) in enumerate(sentences)
                         if self._sentence_score(s, query, priors) > 0), reverse=True)
        # dedupe on normalized text — overlapping chunks or docs stating the same
        # fact (seed + package both cover library hours) must not double-print it
        selected, seen_norm = [], set()
        for signal, score, _, s in scored:
            norm = " ".join(re.findall(r"\w+", s.casefold()))
            if norm in seen_norm or any(norm in p or p in norm for p in seen_norm) \
                    or score < 0.65 * scored[0][1]:
                continue
            if selected and scored[0][0] and not signal:
                continue
            seen_norm.add(norm)
            selected.append(s)
            if len(selected) == 2:
                break
        return " ".join(selected)


class OllamaEmbedder:
    """Text embeddings via the local Ollama embedding API (loopback only).
    Vectors persist to a small JSON cache keyed by sha256(model + text) so
    restarts skip re-embedding the whole corpus; a cache file is optional."""

    def __init__(self, model=None, url="http://127.0.0.1:11434/api/embed", timeout=None,
                 cache_path=None):
        self.model = model or os.environ.get("CORTEX_EMBED_MODEL", "qwen3-embedding:0.6b")
        timeout = timeout if timeout is not None \
            else float(os.environ.get("CORTEX_EMBED_TIMEOUT", "60"))
        self.url, self.timeout = url, timeout
        self._cache = None        # lazily warm-loaded from disk
        self._dirty = False
        self._cache_path = (Path(__file__).resolve().parent / "data" / "embed-cache.json") \
            if cache_path is None \
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

    # Cosine thresholds are calibrated at index build, not hard-coded: absolute
    # similarity values are model- and corpus-dependent (0.6b on 570 docs clusters
    # far tighter than 8b on 68). Off-topic probes measure this deployment's
    # noise ceiling, and the routing floors are derived from it.
    _PROBES = ("How do I bake sourdough bread at high altitude?",
               "What is the best way to train for a marathon?",
               "Explain quantum entanglement to a child.",
               "What were the causes of the French Revolution?")
    _FLOOR_MARGIN = 0.04    # abstain floor = noise ceiling + margin
    _WEAK_MARGIN = 0.20     # close races below floor+this still clarify
    _MULTI_MARGIN = 0.08    # secondary domains must clear floor+this
    _CLOSE_GAP = 0.08       # top-2 within this band = a genuine race

    def __init__(self, documents, embedder=None):
        super().__init__(documents)
        self.embedder = embedder or OllamaEmbedder()
        entries = [(domain, chunk)
                   for domain, body in self.documents.items() for chunk in body["chunks"]]
        vectors = self._embed([f"{chunk['title']}. {chunk['text']}" for _, chunk in entries])
        self._vectors = [(domain, chunk, vector) for (domain, chunk), vector in zip(entries, vectors)]
        self._calibrate()

    def _calibrate(self):
        """Measure the embedding model's off-topic noise ceiling against this
        corpus and derive the abstain/weak/multi floors from it."""
        ceiling = 0.0
        for probe in self._PROBES:
            pv = self.embedder(probe)
            ceiling = max(ceiling, max((cosine(pv, vec) for _, _, vec in self._vectors), default=0.0))
        self.floor = ceiling + self._FLOOR_MARGIN        # below -> no evidence anywhere
        self.weak_top = self.floor + self._WEAK_MARGIN   # weak tops + close races -> clarify
        self.multi_floor = self.floor + self._MULTI_MARGIN  # secondaries must be real hits
        obs.event("CALIBRATION", noise_ceiling=round(ceiling, 3), abstain_floor=round(self.floor, 3))

    def _embed(self, texts):
        """Batch embed when the embedder supports it (one HTTP call); else serial."""
        batch = getattr(self.embedder, "batch", None)
        return batch(texts) if batch else [self.embedder(t) for t in texts]

    def add_source(self, domain, doc_id, title, content, fmt="txt", file=None, metadata=None, relative_path=None):
        super().add_source(domain, doc_id, title, content, fmt=fmt, file=file,
                           metadata=metadata, relative_path=relative_path)
        new_chunks = [c for c in self.documents[domain]["chunks"] if c["doc_id"] == doc_id]
        vectors = self._embed([f"{c['title']}. {c['text']}" for c in new_chunks])
        self._vectors.extend((domain, chunk, vector) for chunk, vector in zip(new_chunks, vectors))

    def remove_source(self, domain, doc_id):
        removed = super().remove_source(domain, doc_id)
        if removed:
            self._vectors = [v for v in self._vectors
                             if not (v[0] == domain and v[1].get("doc_id") == doc_id)]
        return removed

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
                    "options": [{"domain": d, "title": self.documents[d]["title"]} for d in available[:len(DOMAINS)]],
                    "message": SMALLTALK_MESSAGES[kind]}
        ordered = sorted(((s, d) for d, s in self.scores(query, available).items()), reverse=True)
        top = ordered[0][0] if ordered else 0.0
        if top < self.floor:
            if is_vague(query):
                return {"action": "clarify", "tasks": [],
                        "options": [{"domain": d, "title": self.documents[d]["title"]} for d in available[:len(DOMAINS)]],
                        "message": "Could you tell me a bit more about what you need?"}
            return {"action": "unsupported", "tasks": [], "options": [],
                    "message": "This does not match any configured knowledge area."}
        close = [(s, d) for s, d in ordered[:3] if top - s <= self._CLOSE_GAP]
        if len(close) > 1 and top < self.weak_top:
            # same evidence-probe as the keyword classifier — let the documents
            # vote before bothering the user
            viewer = kwargs.get("viewer", "admin")
            probe = []
            for _, d in ordered[:5]:
                hits = self.retrieve(d, query, limit=1, viewer=viewer, query=query)
                probe.append((self._probe_score(query, hits[0] if hits else None), d))
            probe.sort(reverse=True)
            if probe[0][0] > 0 and (len(probe) == 1 or probe[0][0] >= 1.2 * probe[1][0]):
                winner = probe[0][1]
                return {"action": "route", "options": [], "message": "", "evidence_resolved": True,
                        "tasks": [{"domain": winner,
                                   "instruction": f"Answer the {self.documents[winner]['title']} part using only retrieved sources."}]}
            return {"action": "clarify", "tasks": [],
                    "options": [{"domain": d, "title": self.documents[d]["title"]} for _, d in close],
                    "message": "Which area should I check?"}
        # The top domain always routes once it clears the calibrated floor —
        # multi_floor only gates secondaries, which also need keyword evidence
        # because embedding similarity alone is too noisy to join a topic.
        query_tokens = self._query_terms(query)
        strong = [(s, d) for s, d in ordered
                  if d == ordered[0][1]
                  or (s >= self.multi_floor and secondary_confirmed(query_tokens, self.documents[d]["keywords"]))]
        return {"action": "route", "options": [], "message": "",
                "tasks": [{"domain": d,
                           "instruction": f"Answer the {self.documents[d]['title']} part using only retrieved sources."}
                          for _, d in strong]}

    def retrieve(self, domain, instruction, limit=3, viewer="admin", query=None):
        if not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError("limit must be 1..10")
        if domain not in self.documents:
            raise ValueError("Unknown domain")
        instruction = query or instruction
        with obs.stage("RETRIEVAL", domain=domain, engine="semantic", top_k=limit) as out:
            qv = self.embedder(instruction)
            ranked = sorted(((cosine(qv, vec), chunk["doc_id"], chunk["index"], chunk)
                             for d, chunk, vec in self._vectors
                             if d == domain and self.version_eligible(d, chunk, instruction)
                             and self._visible(d, chunk, viewer)),
                            key=lambda item: (-item[0], item[1], item[2]))
            hits = []
            for score, _, _, chunk in ranked:
                if score < self.floor:
                    break
                hits.append({"doc_id": chunk["doc_id"], "title": chunk["title"], "chunk": chunk["text"],
                             "content": chunk["text"], "tokens": chunk["tokens"], "score": score})
                if len(hits) == limit:
                    break
            # cosine says "topically close" but adversarial off-corpus queries are
            # written with campus vocabulary and still embed near real docs — the
            # IDF coverage floor (softer than keyword mode, paraphrases deserve
            # slack) is the abstention check at the evidence layer
            if hits and self.coverage(query or instruction, hits) < 0.35:
                out.update(coverage="below_floor")
                hits = []
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
