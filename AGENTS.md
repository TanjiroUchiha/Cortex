# Cortex — One Front Door for Everything

Domain-routed RAG assistant for the Student Edition challenge: one assistant that routes questions
to the right knowledge area (IT, HR, fees, facilities, general), handles several topics in one
chat, and asks to clarify when unsure.

## Files

- `m1.py` — domain router contract: `route` / `clarify` / `unsupported` / `handoff`, strict JSON
  schema, deterministic validation, `keyword_scores` confidence helper, smalltalk vocabulary
  (`smalltalk()`/`SMALLTALK_MESSAGES` — moved here so the orchestrator can detect them),
  Ollama (Qwen3:4B) router.
- `orchestrator.py` — concurrent domain dispatch, deterministic confidence gate
  (clarify → handoff after `clarify_limit`; a single-domain scope is itself the
  clarification — the ambiguity guard skips it), optional `assistant` hook for
  real model replies on smalltalk/unsupported turns (smalltalk never burns a
  clarify attempt), domain `contacts`/`helpdesk` woven into no_evidence, handoff
  and unsupported messages, deterministic `ambiguous_item_options` guard
  (card/badge-style ambiguity overrides a confident-looking model route), timeouts, bounded
  retries with stable idempotency IDs, local `merge_answers` / `verify_grounding` fallbacks,
  feedback counters.
- `store.py` — `CorpusIndex`: keyword/IDF retrieval over per-domain documents. Each domain
  may carry a `contact` escalation line (`data/corpus.json`) appended to its skill
  answers and reused by the orchestrator's no_evidence/handoff messages. Split into
  paragraph-packed chunks (~1200 chars) at index time; query tokens expand with simple
  `smalltalk()` short-circuits pure greeting/meta/thanks/farewell/ack queries to a clarify
  with the full domain menu — embedding openers produced noise routes ("who are you" → an HR
  policy paragraph). Query tokens expand with simple
  singular/plural variants (`m1.expanded_tokens`, mirrors the frontend rule); multi-domain
  routes keep every domain that clears the 0.45×top bar, not just the top three;
  deterministic fallback classifier;
  domain skill handlers that abstain (`no_evidence`) instead of guessing and dedupe citations
  by doc_id when several chunks of one document hit. `SemanticIndex` (drop-in subclass):
  Qwen3-Embedding-0.6B via Ollama `/api/embed`, per-chunk cosine retrieval + hybrid
  semantic/keyword domain scoring, abstain/clarify thresholds. Corpus embeddings are batched
  into one `/api/embed` call at index build (`OllamaEmbedder.batch`, per-text fallback); every
  synchronous embed call inside async code (skill handlers, `KeywordRouter`, confidence scorer)
  runs via `asyncio.to_thread` so it never blocks the loop. `OllamaEmbedder` persists vectors
  to `data/embed-cache.json` keyed by `sha256(model + text)` — restarts skip re-embedding the
  corpus (startup drops from ~40s to instant), a model change or corrupted cache just
  re-embeds, `cache_path=False` disables it. `load_documents` also merges
  `data/corpus.d/<domain>/*` — `.md`/`.txt` plus `.pdf` and `.docx` extracted via
  `extract_text` (pypdf / python-docx); `python store.py` validates the merged corpus.
  corpus.d sources carry `file` (the on-disk path) so `/corpus/file/{doc_id}` can serve the
  original download. `add_source` inserts a document at runtime and re-indexes it (used by
  `/corpus/upload`; with `persist_root` the API also writes the file into `corpus.d/`).
- `services.py` — `HTTPHandler` (loopback/HTTPS rules, no redirects, no credential forwarding),
  `OllamaRouter`, `KeywordRouter` (offline demo router), `OllamaMerger` (LLM M2 prose with
  deterministic citation computation + deterministic fallback), `OllamaVerifier` (LLM V1:
  model verdict is *combined with* deterministic grounding — a deterministic
  `ungrounded_citation` always fails the response even if the model passes it; malformed
  model output or transport failure falls back to the deterministic verdict),
  `local_pipeline`, `demo_services`, `load_services`, `load_domain_metadata`,
  `OllamaAssistant` (conversational replies for smalltalk/unsupported turns —
  JSON-format-forced so no chain-of-thought leaks; fails open to the
  deterministic messages when the model is unreachable).
  Model names come from env with safe defaults: `CORTEX_M1_MODEL`,
  `CORTEX_EMBED_MODEL`, `CORTEX_MERGE_MODEL`, `CORTEX_V1_MODEL` (default `qwen3:4b` /
  `qwen3-embedding:8b`). `CORTEX_EMBED_TIMEOUT` (default 60s; the corpus batch call
  gets 4×) bounds each embedding request — sized for an 8b on CPU.
- `api.py` — `POST /query`, `POST /query/stream` (SSE: `stage`/`skill` telemetry events, then the
  final `result` event with the same payload as `/query`; `error` events carry a human
  `hint` — e.g. "is Ollama running / is the model pulled" — always HTTP 200),
  `POST /feedback`, `GET /health`, `/domains`, `/metrics`, `/contracts`, `/models`
  (which model powers each stage + the env override names),
  `POST /corpus/upload` (multipart `domain` + `file`; extension allowlist,
  10 MB file cap, 50k char budget, extraction errors → 422; index + persisted file),
  `GET /corpus` (the real knowledge base the UI's corpus browser renders — 503 in
  live mode where remote services own their corpora),
  `GET /corpus/file/{doc_id}` (original uploaded/indexed file download, 404 when the
  source has no file on disk). Every request appends a JSONL line to
  `data/queries.jsonl` (ts, request_id, query, status, domains, elapsed_ms — real usage
  becomes labeled routing data); a `handoff` result also writes a ticket record
  (CTX-NNNN) to `data/tickets.json` and returns its id in `result.ticket`. Per-domain
  routing counters live in `/metrics` as `route.<domain>`.
- `dataset.py` / `evaluate_m1.py` — routing dataset validation and accuracy scoring.
- `data/corpus.json` — seed corpus (5 domains, 14 docs, synthetic, not authoritative) plus
  54 drop-in docs in `data/corpus.d/` (68 sources total, 13–14 per domain — still authored
  demo content, not audited institutional policy).
- `data/starter.json` — 76 labeled routing records (28 train / 18 eval / 12 test / 18 heldout),
  unreviewed. The heldout split was authored after prompt iteration began — the most honest
  accuracy signal, though it has since been iterated against too.
- `baseline-eval-domains.json` — first Ollama baseline: 0.58 exact match.
  `baseline-eval-domains-v6.json` + `baseline-eval-domains-test-v2.json` — raw model:
  12/12 valid, 1.0 exact match on both splits. `baseline-eval-guarded-{v1,test-v1}.json` —
  same scores with orchestrator guards applied (`--guarded` flag measures the system-level
  decision). `baseline-eval-heldout-v6.json` — fresh heldout split: 18/18, 1.0 exact match
  (journey: 0.667 → 0.944 → 1.0 across prompt + guard iterations). Caveat: everything is
  synthetic and has been iterated against — real-world evidence needs real user queries.
- `Cortex-RAG-Plan.{md,html,pdf}` — current plan with flowcharts.
- `archive/` — pre-pivot artifacts (fine-tuning pipeline, GPU notebook, old plan). Not needed
  for this challenge: the problem statement allows keyword/small-model routing.
- `.devin/services.example.json` — live-mode service config template + domain metadata.
- `auth.py` — account layer: SQLite `data/auth.db` (users: id/email/name/password_hash/
  role/auth_provider/is_active), argon2 password hashing, HS256 JWTs (60 min, `iss`
  pinned, role re-read from the DB per request so demotion is immediate), in-memory
  login rate limiter, FastAPI deps `require_user`/`require_role("admin")`.
  `CORTEX_API_TOKEN` remains as a service-token bearer resolving to a synthetic admin.
- `test_auth.py` — tests: login/JWT tampering (expired, forged, wrong-iss,
  alg=none, cosmetic role claim), deactivation, role gates, rate limit, env bootstrap.

## Auth

- Tiers: `guest` (stateless 20-min JWT via `POST /auth/guest`; chat only, pinned to the
  `general` domain server-side, 10 queries/hour/IP — minting fresh tokens doesn't
  reset it), `user` (`/auth/signup` or admin-created; adds `/feedback`, no cap),
  `admin` (+ corpus upload, metrics, models, contracts, `/admin/users*`).
  Public: `/health`, `/auth/*`, `/corpus`, `/corpus/file`. Backend enforces —
  frontend hiding is cosmetic only.
- Conversations: `conversations` table keyed `(user_id, session_id)` —
  `GET/PUT/DELETE /conversations` (user+; guests 403 and stay ephemeral on
  localStorage). `app.js` syncs signed-in sessions with a 600ms debounce;
  server wins on load, localStorage is the offline cache, one-time local→remote
  migration for new accounts.
- Env: `CORTEX_JWT_SECRET` (≥32 chars; live mode refuses to start without it, demo
  uses an ephemeral key + warning), `CORTEX_ADMIN_EMAIL` /
  `CORTEX_ADMIN_PASSWORD` (first-run bootstrap; without them a random-password
  `admin@cortex.local` is created and printed once), `CORTEX_AUTH_DB` (db path).
  `run.py` also loads `Cortex/.env` into M1's environment — real env vars win.
- Frontend: `login.html` (sign-in ⇄ signup toggle + guest button) → JWT in
  `sessionStorage` → `app.js` attaches `Authorization: Bearer` on every call
  (SSE already uses `fetch`+`getReader`, so no token ever lands in a URL);
  401 → back to `/login`. Bump the `?v=` asset query in index/login/landing
  when editing frontend files — browsers cache by that string and stale JS
  silently looks like a broken feature.
- Limitations: bearer tokens can't be revoked mid-flight (no blacklist — logout is
  client-side), sessionStorage is XSS-readable, rate limits are per-process, M2 on
  loopback is unauthenticated, localhost runs without TLS.

## Observability (`obs.py`)

- One logger (`cortex`), structured key=value events (or JSON via `LOG_FORMAT=json`).
  Every event carries `request_id` — minted by the HTTP middleware
  (`REQUEST_START`/`RESPONSE_READY`/`REQUEST_END`) and reused as the engine's own
  `request_id`, so logs, `queries.jsonl`, `/feedback`, and tickets share one id.
  After auth resolves, events also carry `user_id`/`role`. Contextvars isolate
  concurrent async/gather requests; logging failures never propagate.
- Stage lifecycle via `obs.stage("NAME", **meta) as out`: `NAME_START`,
  `NAME_END` (`latency_ms` + fields the caller put in `out`, `state=ok|fail`),
  `NAME_ERROR` (traceback). Stages: `M1` (router), `EMBEDDING`, `RETRIEVAL`
  (score min/max/avg; `LOG_RETRIEVED_DOCS=true` adds doc ids), `M2`, `V1`.
  Point events: `QUERY_RECEIVED`, `CONTEXT_BUILT`, `AUTH_REJECTED`,
  `FLAGS_GENERATED`, `ROUTING_DECISION` (what flags/guards caused),
  `V1_VERDICT`, `PIPELINE_SUMMARY` (per-stage timings one line),
  `PIPELINE_TRACE` (✓/✗/⚠ human trace, DEBUG level).
- Env: `LOG_LEVEL` (DEBUG shows the trace), `LOG_FORMAT`, `LOG_TO_FILE`
  (rotating `logs/app.log`, 5MB×3), `LOG_QUERY_CONTENT`, `LOG_MODEL_OUTPUT`,
  `LOG_RETRIEVED_DOCS` — all default to safe/off. Secrets-shaped field names
  (password/token/key/etc.) are auto-redacted; query text logs as sha256-12
  unless `LOG_QUERY_CONTENT=true`.
- Trace one request: `python run.py 2>&1 | grep "request_id=<rid>"` (or
  `Select-String "request_id=" logs/app.log` with `LOG_TO_FILE=true`).

## Commands (Python 3.10+)

- `python -m unittest discover -s tests -v` — run all tests
- `python dataset.py` — validate routing data
- `python store.py` — validate the merged corpus (sources, retrieval chunks, warnings)
- `python api.py --mode demo --port 8000` — loopback demo API. Default `--retrieval semantic`
  uses qwen3-embedding:8b via Ollama and auto-falls back to keyword if unavailable;
  `--retrieval keyword` forces the no-model path. `--llm-route` uses Qwen3:4B as the M1 router
  (~10-15s/query on CPU, real multi-domain intent understanding — deterministic guards still
  apply). `--llm-merge` swaps deterministic M2 for Qwen3:4B merged prose (slow on CPU — ~60s;
  ~5s on GPU). `--llm-verify` upgrades V1 to the LLM verifier (deterministic citation check
  always still applies). `--m1-backcheck` (implies `--llm-route`) adds a second model call that
  restates the question from the decision JSON alone; cosine(restatement, query) below
  `CORTEX_M1_BACKCHECK_MIN` demotes the route to clarify. `--llm-answer` lets
  `CORTEX_ANSWER_MODEL` (default qwen3:4b) write each domain skill's prose from retrieved
  chunks — citations/evidence stay code-computed. Counters + feedback persist to `data/metrics.json` (gitignored);
  `/corpus/upload` files persist to `data/corpus.d/<domain>/`. `CORTEX_API_TOKEN` locks every
  endpoint behind Bearer auth (the UI has no engine of its own — it stays offline
  without a reachable API).
  Docs at `http://127.0.0.1:8000/docs`
- `python m1.py "How do I reset my password?"` — Qwen3:4B routing decision only (needs Ollama)
- `python evaluate_m1.py --backend ollama --split eval --output <new-file>.json` — routing
  accuracy report; refuses existing output paths. `--guarded` applies the orchestrator's
  deterministic guards (ambiguity + clarify→handoff) before scoring — the system-level metric.
- `python tune_backcheck.py --split train,eval` — M1 back-check threshold tuner: routes each
  labeled query with OllamaRouter, restates from the decision JSON, records the query↔
  restatement cosine, and sweeps thresholds (kept-correct + caught-wrong). Writes
  `data/backcheck-tuning.json`. Needs Ollama; ~20s/record on CPU.

## Contracts

- Domain skill: `POST {url}` with `{request_id, request, domain, instruction}` →
  `{"answer": str, "citations": [{"doc_id","title"}], "evidence"?: [{"doc_id","chunk"}]}`.
  Return error `no_evidence` when retrieval finds nothing — abstain, never guess.
- M2: `{request_id, request, domain_answers, failures}` → `{"response","citations"}`.
  `OllamaMerger` lets the model write the prose only; citations are always the deduped
  union of domain-answer citations, computed in code.
- V1: `{..., "response","citations"}` → `{"status":"passed|failed|uncertain","flags":[],"explanation":""}`.
- Full examples at `/contracts`. `local_only` permits loopback services only.
- Live mode: copy `.devin/services.example.json` → `.devin/services.local.json`, enable real
  endpoints. `domain_metadata` supplies scorer keywords. Secrets via `CORTEX_...` env vars.
  Routed specialists always fan out concurrently (`asyncio.gather`); each remote service is
  its own `resource_group` → own semaphore → true parallel. Services sharing a group name
  serialize (use one group per box). `CORTEX_MAX_DISPATCH` (default 5) caps total
  concurrent calls.

## Metrics (rubric)

- Routing accuracy: `evaluate_m1.py` on labeled eval/test splits; real traffic is logged
  to `data/queries.jsonl` for offline accuracy review.
- Resolution rate: `POST /feedback {request_id, resolved}` → `/metrics`. Counters
  (aggregate + `route.<domain>` per-domain) and feedback persist to `data/metrics.json` —
  warm-loaded on restart. Handoffs append ticket records to `data/tickets.json`.

## Known limitations

- Corpus is synthetic (14 seed docs) plus 54 authored drop-in docs in `data/corpus.d/` —
  including one real `.docx` per domain exercising the python-docx extraction path —
  realistic institutional voice but still demo data, not audited policy. Documents are
  chunked at index time, so real docs drop in directly — a large corpus would still want
  FAISS/Azure AI Search behind the same `retrieve`/`scores` contract.
  `qwen3-embedding:8b` must be pulled for semantic mode: `ollama pull qwen3-embedding:8b`.
- Semantic multi-topic join requires keyword confirmation — embedding similarity alone is
  too noisy at this corpus size (baseline cosine ~0.3-0.5 even for off-domain text).
- Answer generation is extractive by default (returns source sentences). `--llm-answer`
  swaps in `OllamaAnswerer` (`CORTEX_ANSWER_MODEL`, default `qwen3:4b`): the model rewrites
  retrieved chunks into 1–3 sentence prose behind the same `{answer, citations, evidence}`
  contract — citations/evidence stay code-computed, and any failure or `NOT_COVERED`
  output falls back to the extractive answer.
- Confidence = deterministic keyword-evidence share; model self-confidence is never trusted.
- The back-check verifies content preservation, not domain choice — measured on 46 labeled
  records (`data/backcheck-tuning.json`): wrong-domain routes scored 0.721–0.854 vs correct
  0.738–0.882. A second signal (query↔per-domain exemplar similarity, `tune_exemplar.py`)
  was tested and rejected: wrong routes scored 0.47–0.65 but correct ones dip to 0.30 —
  no separating threshold exists at this corpus size. Wrong-domain defense stays with the
  deterministic keyword-evidence gate + ambiguity guards.
- Baseline eval is a small synthetic set — not evidence of real-world routing quality.

## Frontend (API client — no local engine)

- The frontend lives in the **sibling `../frontend/` directory** (repo layout:
  `backend/` + `frontend/`; `api.py` auto-resolves it, falling back to
  `backend/frontend/` when backend is shipped standalone).
- `../frontend/landing.html` + `landing.css` (landing page at `/` — fetches `/health`,
  `/corpus` and `/metrics` for live stats), plus `index.html`, `styles.css`, and `app.js`
  (the assistant at `/app`) are plain HTML/CSS/JavaScript, with no
  build step or runtime package dependencies. The UI is product-facing (no demo/simulation
  labels — per user direction). Google Fonts has system-font fallbacks.
- The backend is the only engine. The frontend contains **no bundled corpus, no
  classifier, no retrieval, no merge/verify simulation** — `DOMAINS` in app.js holds
  UI metadata (title/color) only; document contents arrive via `GET /corpus`.
  There must never be a code path that answers a query without the API — a dead
  backend shows an honest "Offline" state and send surfaces "backend unreachable".
- The backend serves the UI same-origin (`api.py` mounts `../frontend/` at `/`, with explicit
  routes so `/` → `landing.html` and `/app` → `index.html`) — open
  `http://127.0.0.1:8000/` after starting the API. The page probes `/health` at
  startup: live → queries stream through `POST /query/stream` (SSE `stage`/`skill`/
  `result`/`error` events drive the pipeline UI), feedback posts to `/feedback` with
  the backend `request_id`, `/domains` reconciles titles and new domains into the
  scope chips, `/corpus` fills the corpus browser, the modal gains a live
  `/corpus/upload` drop-zone, and the metrics drawer reads `/metrics` counters +
  `/models` engine list. `?api=<url>` overrides the base URL, but cross-origin posts
  are rejected by `authorize()`; serving via `api.py` is the supported integration.
- Fast checks: `node --check ../frontend/app.js` and `node --test tests/frontend.test.cjs`.
- Browser checks spawn their own backend (`api.py --mode demo --retrieval keyword`,
  ephemeral port — no Ollama needed):
  `npm exec --yes --package=@playwright/test@1.55.1 --call "node tests/frontend.browser.cjs"`.
  The suite uses installed Microsoft Edge by default; `CORTEX_BROWSER` can select another
  installed Playwright channel, and `CORTEX_PREVIEW_URL` points at an already-running
  api.py instead of spawning one.
- Optional accessibility checks, in Git Bash:
  `CORTEX_A11Y=1 npm exec --yes --package=@playwright/test@1.55.1 --package=@axe-core/playwright@4.10.2 --call "node tests/frontend.browser.cjs"`.
  These packages are test tooling fetched into npm's cache, not frontend dependencies.
  `CORTEX_SCREENSHOTS=1` optionally saves review images under `tests/frontend-*.png`.
- Persist evidence and stage snapshots with each assistant message. Citation clicks must
  restore that answer's sources; traces record `engine: "live"` — the backend pipeline.
- `#corpusModal` ("Peek inside") is a corpus browser over the real `/corpus` payload: one
  `.folder-card` per domain, click/Enter fans out its documents. `#themeBtn` is a
  `<button role="switch" aria-checked>` (aria-checked = dark); `#menuBtn` bars animate
  off `aria-expanded`. Animated snippets adapted from Uiverse.io are driven by
  classes/aria state, not hidden checkboxes.
- Off-canvas panels must become visible and non-inert before focusing their contents. Avoid
  visibility transitions on the entering state; they can drop immediate keyboard focus.
  Reduced-motion styles must disable animation delays as well as movement.
