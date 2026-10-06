# Cortex — One Front Door for Everything

A privacy-aware, domain-routed **RAG** assistant. Instead of separate bots for IT,
HR, fees, facilities and general questions, a user asks one assistant — Cortex
works out which department(s) a question belongs to, answers each part from that
department's own documents, merges the sections, verifies the result against its
citations, and abstains instead of guessing when the knowledge base has nothing.

Built for the Student Edition hackathon challenge *"One Front Door for Everything."*

---

## How a question flows

```
You type one message (it can contain several topics)
        │
        ▼
  M1 — Domain Router                     ← qwen3:4b (or deterministic fallback)
  emits a decision: route | clarify | unsupported | handoff
  route = list of tasks {domain, instruction}
        │
        ▼  (tasks fan out in parallel — asyncio.gather)
  Domain RAG skills — it / hr / fees / facilities / general
  each: embed query → retrieve top chunks from ITS corpus only
       → extract answer sentences → return {answer, citations, evidence}
       → or abstain with no_evidence (never guesses)
        │
        ▼
  M2 — Merger                            ← LLM prose or deterministic join
  combines domain answers into one labelled reply, dedupes citations
        │
        ▼
  V1 — Verifier                          ← LLM or deterministic grounding check
  every citation must trace to retrieved evidence;
  verdict drives the final status
        │
        ▼
  Response JSON → Chat UI (sections, citations, evidence, feedback)
```

A worked example — *"the hostel wifi is down, my payslip didn't arrive, and
when are the exam fees due?"*

1. **M1** returns `route` with tasks `[it, hr, fees]` — it splits the *routing*,
   not the text; every skill receives the full query plus its instruction.
2. **Skills** run concurrently. IT retrieves the WiFi doc and extracts
   *"If WiFi is down in a whole area, check the IT status page first…"*;
   HR finds the payslip doc; fees finds the deadline doc.
3. **M2** joins them into `[FEES] … [IT] … [HR] …` and dedupes citations.
4. **V1** checks every cited `doc_id` appears in retrieved evidence.
5. The API returns one payload; the UI renders three labelled sections with
   clickable citations and a feedback widget.

If a part has no coverage (e.g. *"and can I bring my cat to lab"*), that domain
abstains (`no_evidence`), the others still answer, and the reply is honestly
marked `partial` — never a fabricated section.

---

## The RAG part, in plain terms

**RAG = Retrieval-Augmented Generation**: don't let the model answer from memory —
first *retrieve* relevant documents, then *ground* the answer in them.

| Piece | What Cortex uses | Why |
|---|---|---|
| Chunking | Docs split into ~1200-char paragraph chunks at index time | finer retrieval granularity |
| Embeddings | `qwen3-embedding:0.6b` via local Ollama `/api/embed` | maps text to vectors; cosine similarity finds *meaningful* matches ("cant sign in" → "login help") |
| Fallback retrieval | keyword/IDF token overlap (deterministic) | works with zero models — demo/offline path |
| Answer | extractive — real sentences from retrieved chunks | can never invent a fact; citations are literal |
| Optional per-skill prose | `--llm-answer` lets qwen3:4b rewrite the retrieved chunks into fluent 1–3 sentence answers | citations + evidence stay code-computed; a `NOT_COVERED` or dead model falls back to the extractive answer |
| Optional generation | `--llm-merge` lets qwen3:4b write the joined prose | citations are still computed in code — the LLM never invents `doc_id`s |
| Verification | deterministic grounding (authoritative) + optional LLM judge | a citation the evidence doesn't contain = automatic fail |

**Routing specifics that matter:**

- Multi-domain joins require **keyword confirmation** — embedding similarity alone
  is too noisy on a small corpus (baseline cosine ~0.3–0.5 even for off-domain text).
- A secondary domain needs a **topic-bearing keyword**, not a lone place-noun:
  *"the hostel wifi is down"* is an IT problem *located* in a hostel — `LOCATION_WORDS`
  (hostel, room, campus, block…) don't qualify a runner-up domain unless the place
  itself is the topic (2+ place-words, e.g. *"my hostel room is cold"*).
- A `smalltalk()` layer catches greetings, thanks, "who are you", "help" etc.
  *before* embedding — otherwise chit-chat embeds near friendly-sounding docs and
  produces nonsense clarify options.
- `ambiguous_item_options` guards card/badge/projector-style ambiguity
  (payment card vs ID card vs system account) → clarify with domain chips.
- `clarify_attempts` cap: after one failed clarification → honest `handoff`, logged
  as a ticket record (`CTX-NNNN`) in `data/tickets.json` — local record, not an
  external helpdesk submission.
- Optional **back-check** (`--m1-backcheck`, implies `--llm-route`): a second
  model call sees only the decision JSON and restates the question it would
  have answered; cosine(query, restatement) below `CORTEX_M1_BACKCHECK_MIN`
  (0.70) demotes the route to clarify. Catches hallucinated/dropped topics the
  schema can't — fails open if the check itself is unavailable. The threshold is
  tuned on labeled data: `python tune_backcheck.py --split train,eval` routed
  all 46 train+eval records (correct routes scored 0.738–0.882; wrong-domain
  routes overlapped at 0.721–0.854 — the check verifies content preservation,
  not domain choice; keyword-evidence gating handles that). 0.70 keeps every
  correct route with margin.

---

## API surface (FastAPI, same origin as the UI)

| Endpoint | Purpose |
|---|---|
| `POST /query` | full pipeline → result JSON |
| `POST /query/stream` | same, via SSE: `stage`/`skill` telemetry → `result` |
| `POST /feedback` | `{request_id, resolved}` → metrics |
| `GET  /health` | liveness probe (frontend's connect check) |
| `GET  /domains` | domain registry + titles |
| `GET  /corpus` | real corpus contents — the UI's corpus browser reads this; each source carries a `format` shown as a DOCX/PDF/TXT badge |
| `GET  /metrics` | request/clarify/resolved counters + `route.<domain>` routing mix (persisted to `backend/data/metrics.json`) |
| `GET  /models` | which model powers each stage + env override names |
| `GET  /contracts` | service payload schemas |
| `POST /corpus/upload` | add a `.md`/`.txt`/`.pdf`/`.docx` to a domain; indexed instantly, persisted to `dataset/corpus.d/` |
| `GET  /corpus/file/{doc_id}` | download the original file behind a corpus doc (404 when none) |

Auth: `CORTEX_API_TOKEN` (env) → Bearer required on every endpoint; loopback +
same-origin otherwise. Docs at `/docs`.

---

## File structure

```
Cortex/
├── run.py                 # launcher: boots models.m2 + backend.api, opens the UI
├── frontend/              # the client (no engine inside)
│   ├── landing.html       # landing page at / — live health/corpus/metrics stats
│   ├── index.html         # assistant at /app (chat, pipeline panel, corpus browser)
│   ├── login.html         # sign-in / signup / guest
│   ├── styles.css         # dark/light themes, responsive, reduced-motion
│   └── app.js             # pure API client: SSE pipeline, evidence, citations,
│                          #   feedback, history, upload UI
├── backend/               # the engine (FastAPI + pipeline)
│   ├── api.py             # all endpoints, SSE streaming, auth, static mount, CLI
│   ├── auth.py + db.py     # MongoDB Atlas accounts (users/admin_users), argon2, JWT, roles
│   ├── orchestrator.py    # parallel skill dispatch, timeouts/retries, clarify→
│                          #   handoff, confidence gate, merge/verify, metrics
│   ├── store.py           # CorpusIndex (corpus load, chunking, IDF, keyword
│                          #   retrieval, uploads) + SemanticIndex (Ollama
│                          #   embeddings, cosine retrieval, cached vectors)
│   ├── services.py        # HTTPHandler, OllamaRouter/KeywordRouter,
│                          #   OllamaMerger/OllamaVerifier, service loaders
│   ├── corpus_package.py  # expanded-corpus importer + YAML frontmatter parser
│   ├── obs.py             # structured request/pipeline logging (contextvars)
│   ├── service/           # live-mode service config examples
│   └── data/              # runtime state (metrics, queries, tickets,
│                          #   embed-cache — all gitignored)
├── models/
│   ├── m1.py              # M1 router contract: decision schema, parse_decision,
│                          #   keyword_scores, ambiguity guards, smalltalk,
│                          #   PACKAGE_DOMAIN folder→domain merge map
│   ├── v1_checks.py       # V1 deterministic grounding verifier
│   └── m2/                # M2 merger service (FastAPI package: api/service/
│                          #   schemas/safety/demo/fixtures)
├── dataset/
│   ├── corpus.json        # seed corpus (14 docs, synthetic)
│   ├── corpus.d/<category>/ # all document sources (11 folders → 6 domains); uploads land here
│   ├── evaluation/        # 130 eval questions (retrieval/governance/multi/negative)
│   ├── starter.json       # 76 labeled routing records (train/eval/test/heldout)
│   ├── backcheck-tuning.json
│   └── baseline-eval-*.json
├── test/                  # unittest suite + eval/tuning tools
│   ├── test_*.py          # auth, corpus, observability, V1 verifier
│   ├── dataset.py         # routing dataset validation
│   ├── evaluate_m1.py     # routing-accuracy scorer (--guarded = system level)
│   ├── evaluate_corpus.py # 130-question corpus evaluation
│   └── tune_*.py, probe_m2.py, run_queries.py
└── AGENTS.md              # contributor guide (commands, contracts, limitations)
```

## Running it

```bash
python run.py                              # one command: M2 + M1, opens the UI
python -m backend.api --mode demo --port 8000   # landing at :8000 · assistant at /app
python -m backend.api --mode demo --llm-route   # qwen3:4B does the routing (slow on CPU)
python -m backend.api --mode demo --llm-merge --llm-verify  # LLM merger + verifier
python -m unittest discover -s test -v          # backend tests
```

Ollama needs `qwen3-embedding:0.6b` pulled for semantic mode (`ollama pull
qwen3-embedding:0.6b`); without it the API falls back to keyword retrieval.

---

## Ownership — who built what

**This repo (your part):**
- Whole RAG engine: corpus, chunking, retrieval (semantic + keyword), extraction
- M1 router: deterministic `KeywordRouter`/`SemanticIndex.classify`, plus
  `OllamaRouter` behind `--llm-route`; ambiguity/smalltalk/confidence guards
- Orchestrator: parallel dispatch, timeouts, retries, clarify limits,
  abstain/failure semantics, durable metrics, feedback
- M2 + V1: deterministic local implementations (`merge_answers`,
  `verify_grounding`) + optional local-LLM versions (`--llm-merge`, `--llm-verify`)
- Assistant layer: real model replies for greetings/off-corpus questions when a
  chat model is pulled (`OllamaAssistant`, `CORTEX_ASSISTANT=off` disables),
  per-domain escalation contacts in every answer + no_evidence/handoff messages
- API: all 9 endpoints, SSE, auth, corpus upload + persistence
- Frontend: full UI as a pure API client (no local engine — a dead backend shows
  an honest offline state instead of fabricating answers), upload/metrics/models

**Manav & Malay (the live-mode services):**
- Remote M2 merger and V1 verifier (+ optionally remote domain skills) running
  as hosted services. When they ship, their endpoints go into
  `backend/service/services.local.json` (template at `backend/service/services.example.json`)
  and `python -m backend.api --mode live` swaps the local merge/verify for their URLs —
  **no code changes needed**: the orchestrator already calls M2/V1 through the
  same `{response, citations}` / `{status, flags, explanation}` contracts, and
  `validate_result` will reject anything malformed.
- Deliverables owed: endpoint URLs + auth scheme (Bearer via `CORTEX_...` env
  vars, no credentials in URLs — HTTPHandler enforces that), response shapes
  matching `/contracts`, and an SLA sanity check (orchestrator budgets:
  `call_timeout` per call, `total_timeout` per request, 2 attempts).

**Deliberately deferred:** FAISS/vector DB (brute-force cosine is ~1 ms at
64 docs; the `retrieve`/`scores` contract allows swapping later), auth on the
UI (token mode is API-only by design), real (non-synthetic) corpus content.

## Known limitations (honest ones)

- Corpus is authored/synthetic — realistic voice, not audited institutional policy.
- Answers are extractive sentences, not generative prose (by default) — reads
  clipped but can't hallucinate.
- Semantic routing at this corpus size has noise — mitigated by the keyword
  gate + location rule, not eliminated.
- LLM paths (`--llm-route`/`--llm-merge`) take ~11–15 s/query on CPU; they're
  the GPU/production path, not the demo path.
