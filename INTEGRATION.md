# Cortex M1 ↔ M2 integration

## How it fits
M1 (router, orchestrator, domain skills) calls M2 (merger) over HTTP.
Adapter: `M1/services.py` → `RemoteMerger` (`_to_m2_input` is the only place
that maps M1's payload to M2's schema). V1 is still M1's own verifier.

## Start (3 things must be running)
1. Ollama (`ollama list` should show the model named in M2/.env)
2. M2, from the Cortex folder:
   M2\.venv\Scripts\activate
   uvicorn M2.api:app --host 127.0.0.1 --port 9001 --env-file M2\.env
3. M1, from Cortex\M1:
   python api.py --mode demo --retrieval keyword --m2-url http://127.0.0.1:9001/merge

## M2/.env (copy from .env.example)
M2_ENDPOINT=http://127.0.0.1:11434/api/chat
M2_MODEL=<exact name from `ollama list`>
M2_TIMEOUT_SECONDS=50        # M2 retries once; 2x this must stay under M1's 110s
M2_MAX_EVIDENCE_CHARS=1000

## Verify
cd M1 && python probe_m2.py
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