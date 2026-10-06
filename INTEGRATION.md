# Cortex M1 ↔ M2 integration

## How it fits
M1 (router, orchestrator, domain skills) calls M2 (merger) over HTTP.
Adapter: `backend/services.py` → `RemoteMerger` (`_to_m2_input` is the only place
that maps M1's payload to M2's schema). V1 is still M1's own verifier.

## Account store (MongoDB Atlas)

Accounts, roles, per-user conversations and upload provenance live in Atlas —
`backend/db.py` builds the `mongodb+srv://` URI from `.env`
(`MONGO_USERNAME` / `MONGO_PASSWORD` / `MONGO_CLUSTER`; `MONGO_DB` defaults to
`cortex`). Collections: `users`, `admin_users` (the role split — role is where
the record lives, not a field), `conversations`, `documents`. Startup fails
with a clear error when the keys are missing or Atlas is unreachable (check
Atlas → Network Access for your IP). Seed admins with
`scripts/create_admin.py`; the legacy SQLite `auth.db` lifts over with
`scripts/migrate_auth_to_mongo.py`. Tests never touch Atlas — they inject
`AuthDB.memory()`.

## Start (3 things must be running)
1. Ollama (`ollama list` should show the model named in models/m2/.env)
2. M2, from the Cortex folder:
   models\m2\.venv\Scripts\activate
   uvicorn models.m2.api:app --host 127.0.0.1 --port 9001 --env-file models\m2\.env
3. M1, from the Cortex folder:
   python -m backend.api --mode demo --retrieval keyword --m2-url http://127.0.0.1:9001/merge

## models/m2/.env (copy from .env.example)
M2_ENDPOINT=http://127.0.0.1:11434/api/chat
M2_MODEL=<exact name from `ollama list`>
M2_TIMEOUT_SECONDS=50        # M2 retries once; 2x this must stay under M1's 110s
M2_MAX_EVIDENCE_CHARS=1000

## Verify
python test/probe_m2.py
ONE domain -> 200 instantly (no model call)
TWO domains -> 200 in a few seconds (model call)

## When it breaks
- `remote_request_rejected` in M1 = M2 returned 4xx (schema). Run probe_m2.py.
- `remote_unavailable` / `transport_unavailable` = M2 down, 5xx, or too slow.
- Port 9001 in use: netstat -ano | findstr :9001, then taskkill /PID <id> /F
- M1 falls back to its local merge on any M2 failure, so users still get an answer.

## Gotchas
- M1 and M2 must run on the same machine (privacy=local_only needs loopback).
- M2 request must be a plain string; the adapter converts M1's request object.
- Ollama request needs "think": False or reasoning models stall.
- If you use --llm-route / --llm-verify / --llm-answer on M1, set the model first:
  set CORTEX_M1_MODEL=<name from ollama list>

## One-command launcher (run.py)

From the repo root, `run.py` starts M2, then M1, waits until the API is answering,
and opens the UI. Ctrl+C stops everything it started.

    python run.py                        # M2 + M1  → http://127.0.0.1:8000/
    python run.py --no-m2                # M1 only (deterministic local merge)
    python run.py --llm-route --llm-merge
    python run.py --frontend-port 5500   # UI served separately on :5500 (see CORS below)

M2 runs under `models/m2/.venv` when one exists and reads `models/m2/.env` directly (no
python-dotenv needed); the API runs under `backend/.venv` when present, else the
launcher's own interpreter. Ollama is optional — without it M1 falls back to
keyword retrieval and the deterministic merge.

## Separate-origin UI (CORS)

By default the API is same-origin only (`authorize()` rejects other origins). To
serve the UI from a different origin — e.g. the VS Code Live Server — allowlist it:

    python -m backend.api --mode demo --allow-origin http://127.0.0.1:5500
    # or: set CORTEX_ALLOWED_ORIGINS=http://127.0.0.1:5500

then open the assistant with the API base set:

    http://127.0.0.1:5500/index.html?api=http://127.0.0.1:8000

`run.py --frontend-port 5500` does both steps for you. Only the configured
origins are allowed (never a wildcard); with nothing configured the API stays
same-origin.