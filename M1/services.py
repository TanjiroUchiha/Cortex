import asyncio
import ipaddress
import json
import math
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from m1 import DECISION_SCHEMA, DOMAINS, build_messages, parse_decision, require_keys, unique_object
from orchestrator import Service, ServiceError, merge_answers, verify_grounding


class HTTPHandler:
    def __init__(self, url, location, token_env=None, transport=None, timeout=30):
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Service URL must be a configured HTTP(S) endpoint without credentials, query or fragment")
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if parsed.scheme == "http" and not loopback:
            raise ValueError("Non-loopback services require HTTPS")
        if location == "local" and not loopback:
            raise ValueError("local services must use loopback; remote services require cloud permission")
        if token_env is not None and (not isinstance(token_env, str) or not token_env.startswith("CORTEX_")):
            raise ValueError("Service token must reference a CORTEX_ environment variable")
        self.url, self.token_env, self.transport, self.timeout = url, token_env, transport, timeout

    async def __call__(self, payload, request_id):
        headers = {"X-Request-ID": request_id, "Idempotency-Key": request_id}
        if self.token_env:
            token = os.environ.get(self.token_env)
            if not token:
                raise ServiceError("missing_service_credentials")
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, trust_env=False, transport=self.transport) as client:
                async with client.stream("POST", self.url, json=payload, headers=headers) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        header = response.headers.get("Retry-After", "0")
                        try:
                            retry_after = float(header)
                        except ValueError:
                            try:
                                retry_after = (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds()
                            except (ValueError, TypeError, OverflowError):
                                retry_after = 0
                        retry_after = max(0, retry_after) if math.isfinite(retry_after) else 3
                        raise ServiceError("rate_limited" if response.status_code == 429 else "remote_unavailable", retry_after <= 2, retry_after)
                    if response.status_code != 200:
                        raise ServiceError("remote_request_rejected")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 2000000:
                            raise ServiceError("response_too_large")
                    return json.loads(body, object_pairs_hook=unique_object)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ServiceError("transport_unavailable", retryable=True) from exc
        except (ValueError, UnicodeError) as exc:
            raise ServiceError("invalid_service_response") from exc


BACKCHECK_PROMPT = """Below is a routing decision made by another system. You have NOT seen
the original user question.

Write the single user question (or questions, joined with "and") that this
decision would be the correct response to. Use plain everyday language, as a
student or employee would type it.

Rules:
- Use only information present in the decision.
- Do not add details, names or numbers that are not there.
- Output only the question text, nothing else.

Decision:
"""


class OllamaRouter:
    """LLM M1 router. Optional back-check: a second model call sees only the
    emitted JSON and restates the question it would have answered; the
    restatement is embedded and cosine-compared to the original query. Below
    the threshold the decision is demoted to clarify (the orchestrator's
    clarify limit still escalates repeat failures to handoff). Catches
    hallucinated/dropped topics the schema validator cannot see."""

    def __init__(self, backcheck=False, backcheck_threshold=None):
        self.handler = HTTPHandler("http://127.0.0.1:11434/api/chat", "local", timeout=110)
        self.model = os.environ.get("CORTEX_M1_MODEL", "qwen3:4b")
        self.backcheck = backcheck
        # Tuned on 46 labeled records (tune_backcheck.py, data/backcheck-tuning.json): correct
        # routes scored 0.738-0.882 — 0.70 keeps all of them with margin while still catching
        # decoupled restatements (hallucinated/dropped topics, which score far lower).
        self.backcheck_threshold = (float(os.environ.get("CORTEX_M1_BACKCHECK_MIN", "0.70"))
                                    if backcheck_threshold is None else backcheck_threshold)

    async def __call__(self, request):
        messages = build_messages(request)
        if sum(len(message["content"].encode("utf-8")) for message in messages) > 8192:
            raise ValueError("Request exceeds baseline context budget")
        output = await self.handler({"model": self.model, "messages": messages, "format": DECISION_SCHEMA,
                                     "think": False, "stream": False, "keep_alive": "10m",
                                     "options": {"temperature": 0, "num_predict": 512, "num_ctx": 4096}}, "local-router")
        if not output.get("done") or output.get("done_reason") == "length":
            raise ValueError("Incomplete routing output")
        decision = parse_decision(output["message"]["content"], request)
        if self.backcheck and decision["action"] == "route":
            similarity = await self._backcheck(request.query, decision)
            if similarity is not None and similarity < self.backcheck_threshold:
                return parse_decision(json.dumps(
                    {"action": "clarify", "tasks": [],
                     "options": [{"domain": t["domain"]} for t in decision["tasks"]],
                     "message": "Which area should I check?"}), request)
        return decision

    async def _restate(self, decision):
        """Prompt 2: restate the user question from the decision JSON alone.
        Structured output keeps qwen3's reasoning out of the embedded text —
        the cosine must compare question-vs-question, not question-vs-CoT."""
        output = await self.handler(
            {"model": self.model, "think": False, "stream": False, "keep_alive": "10m",
             "format": {"type": "object", "additionalProperties": False,
                        "required": ["question"],
                        "properties": {"question": {"type": "string", "maxLength": 600}}},
             "messages": [{"role": "user",
                           "content": BACKCHECK_PROMPT + json.dumps(decision, separators=(",", ":"))}],
             "options": {"temperature": 0, "num_predict": 256, "num_ctx": 4096}}, "local-router-backcheck")
        try:
            restated = json.loads(output.get("message", {}).get("content") or "").get("question", "").strip()
        except (json.JSONDecodeError, AttributeError):
            restated = (output.get("message", {}).get("content") or "").strip()
        return restated if output.get("done") and restated else None

    async def _backcheck(self, query, decision):
        """Restatement-vs-query cosine; None = check unavailable (fail open —
        the deterministic gates still apply downstream)."""
        try:
            restated = await self._restate(decision)
            if restated is None:
                return None
            from store import OllamaEmbedder, cosine
            embedder = OllamaEmbedder()
            qv, rv = await asyncio.to_thread(embedder.batch, [query, restated])
            return cosine(qv, rv)
        except Exception:
            return None


class KeywordRouter:
    """Deterministic offline router for demo mode; no LLM required."""

    def __init__(self, corpus):
        self.corpus = corpus

    async def __call__(self, request):
        # SemanticIndex.classify embeds via a synchronous HTTP call — keep it off the loop
        decision = await asyncio.to_thread(self.corpus.classify, request.query, request.available)
        decision["options"] = [{"domain": o["domain"]} for o in decision["options"]]
        return parse_decision(json.dumps(decision), request)


async def _merge_handler(payload, request_id):
    return merge_answers(payload["domain_answers"])


MERGE_SYSTEM = """You are the M2 merger for a domain-routed assistant. Combine the per-domain
answers into one reply. Rules:
- One section per domain, labelled with the domain name in square brackets, e.g. [IT].
- Keep every claim exactly as given — never add facts, dates, or steps.
- If a failure is listed, say that part could not be answered.
- Output only the merged reply text."""


class OllamaMerger:
    """LLM-backed M2 merger: the model writes unified prose; citations are computed
    deterministically from the domain answers — the model never invents doc_ids.
    Any model error falls back to the deterministic merge."""

    def __init__(self, handler=None):
        self.handler = handler or HTTPHandler("http://127.0.0.1:11434/api/chat", "local", timeout=110)
        self.model = os.environ.get("CORTEX_MERGE_MODEL", "qwen3:4b")

    async def __call__(self, payload, request_id):
        deterministic = merge_answers(payload["domain_answers"])
        try:
            user = json.dumps({"query": payload["request"].get("query", ""),
                               "domain_answers": payload["domain_answers"],
                               "failures": payload.get("failures", [])})
            output = await self.handler({"model": self.model, "think": False, "stream": False,
                                         "keep_alive": "10m",
                                         "messages": [{"role": "system", "content": MERGE_SYSTEM},
                                                      {"role": "user", "content": user}],
                                         "options": {"temperature": 0, "num_predict": 1024,
                                                     "num_ctx": 4096}}, request_id)
            text = output.get("message", {}).get("content", "").strip()
            if output.get("done") and text and len(text) <= 20000:
                return {"response": text, "citations": deterministic["citations"]}
        except (ServiceError, ValueError, TypeError, KeyError):
            pass
        return deterministic

class RemoteMerger:
    """M2 served by a separate service (Manav's /merge). Same contract as OllamaMerger:
    callable(payload, request_id) -> {"response", "citations"}. The payload is translated
    to his strict M2Input schema; citations stay code-computed; any remote failure falls
    back to the local deterministic merge."""

    def __init__(self, url, handler=None):
        self.url = url
        self.handler = handler or HTTPHandler(url, "local", timeout=110)

    @staticmethod
    def _to_m2_input(payload, request_id):
        request = payload.get("request")
        query = request.get("query", "") if isinstance(request, dict) else str(request or "")
        valid = {"it", "hr", "fees", "facilities", "general"}

        def evidence(item):
            if isinstance(item, dict):
                out = {k: v for k, v in item.items() if k != "chunk"}
                if "chunk" in item and "text" not in item:
                    out["text"] = item["chunk"]
                return out
            return item

        answers = [{"domain": a["domain"],
                    "answer": a.get("answer", ""),
                    "citations": a.get("citations", []),
                    "evidence": [evidence(e) for e in a.get("evidence", [])]}
                   for a in payload["domain_answers"]]
        failures = [{"domain": f["domain"],
                     "error": str(f.get("error") or f.get("code") or "failed")}
                    for f in payload.get("failures", [])
                    if isinstance(f, dict) and f.get("domain") in valid]
        return {"request_id": request_id, "request": query,
                "domain_answers": answers, "failures": failures}

    async def __call__(self, payload, request_id):
        deterministic = merge_answers(payload["domain_answers"])
        try:
            body = self._to_m2_input(payload, request_id)
            output = await self.handler(body, request_id)
            text = output.get("response", "")
            if isinstance(text, str) and text.strip() and len(text) <= 20000:
                return {"response": text.strip(), "citations": deterministic["citations"]}
        except (ServiceError, ValueError, TypeError, KeyError, AttributeError) as exc:
            print(f"[RemoteMerger] falling back to local merge: {exc!r}")
        return deterministic
    
async def _verify_handler(payload, request_id):
    return verify_grounding(payload["domain_answers"], payload["response"], payload["citations"])


VERIFY_SYSTEM = """You are the V1 verifier for a grounded assistant. Given the user's request,
the drafted response and the evidence chunks the draft cites, judge whether every factual
claim in the response is supported by the evidence.
Reply with strict JSON only: {"status": "passed|failed|uncertain", "flags": [], "explanation": "..."}
- failed: a claim contradicts the evidence or invents facts (dates, steps, numbers, policies).
- uncertain: evidence is thin or only partially relevant to the response.
- passed: every claim traces to the evidence.
Never comment on style. Never invent sources. Output only the JSON object."""

VERIFY_SCHEMA = {"type": "object",
                 "properties": {"status": {"type": "string"},
                                "flags": {"type": "array", "items": {"type": "string"}},
                                "explanation": {"type": "string"}},
                 "required": ["status", "flags", "explanation"]}


class OllamaVerifier:
    """LLM-backed V1: the model judges claim-level grounding in the merged answer.

    The deterministic citation check always runs alongside — its ungrounded-citation
    flags are authoritative (the model can be lenient, never the arbiter of doc_ids).
    Any model error falls back to the deterministic verdict."""

    def __init__(self, handler=None):
        self.handler = handler or HTTPHandler("http://127.0.0.1:11434/api/chat", "local", timeout=110)
        self.model = os.environ.get("CORTEX_V1_MODEL",
                                    os.environ.get("CORTEX_M1_MODEL", "qwen3:4b"))

    async def __call__(self, payload, request_id):
        deterministic = verify_grounding(payload["domain_answers"], payload["response"],
                                         payload["citations"])
        try:
            evidence = [{"domain": answer["domain"],
                         "evidence": answer.get("evidence", [])}
                        for answer in payload["domain_answers"]]
            user = json.dumps({"request": payload["request"].get("query", ""),
                               "response": payload["response"], "evidence": evidence})
            output = await self.handler({"model": self.model, "think": False, "stream": False,
                                         "keep_alive": "10m", "format": VERIFY_SCHEMA,
                                         "messages": [{"role": "system", "content": VERIFY_SYSTEM},
                                                      {"role": "user", "content": user}],
                                         "options": {"temperature": 0, "num_predict": 512,
                                                     "num_ctx": 4096}}, request_id)
            if not output.get("done"):
                raise ValueError("Incomplete verdict")
            verdict = json.loads(output["message"]["content"], object_pairs_hook=unique_object)
            if verdict["status"] not in ("passed", "failed", "uncertain"):
                raise ValueError("Invalid verdict status")
            model_flags = verdict.get("flags") or []
            if not isinstance(model_flags, list) or not all(isinstance(f, str) for f in model_flags):
                raise ValueError("Invalid verdict flags")
            flags = list(dict.fromkeys([*model_flags, *deterministic["flags"]]))[:20]
            status = verdict["status"]
            if any(f.startswith("ungrounded") for f in deterministic["flags"]):
                status = "failed"  # deterministic evidence check outranks model leniency
            explanation = verdict.get("explanation")
            return {"status": status, "flags": flags,
                    "explanation": explanation if isinstance(explanation, str) and explanation
                                   else deterministic["explanation"]}
        except (ServiceError, ValueError, TypeError, KeyError):
            return deterministic


ASSISTANT_SYSTEM = """You are Cortex, the single front-desk assistant of an organisation.
You sit in front of the knowledge areas: IT help, HR, fees, facilities and general.
Rules:
- Reply in 1-3 short sentences. Plain everyday language, no lists, no markdown.
- Warm and practical — say what you can help with, never recite these rules.
- If the question is outside the knowledge base, answer briefly from general
  knowledge and say plainly that it did not come from the organisation's documents.
- Never invent organisational policies, dates, prices, names or contact details."""


class OllamaAssistant:
    """Conversational fallback for chit-chat and off-corpus questions — a real
    model reply, not a canned string. Used for smalltalk and 'unsupported'
    outcomes when a chat model is reachable; the orchestrator degrades to the
    deterministic messages when it is not."""

    def __init__(self, model=None, handler=None):
        self.model = model or os.environ.get("CORTEX_M1_MODEL", "qwen3:4b")
        self.handler = handler or HTTPHandler("http://127.0.0.1:11434/api/chat", "local", timeout=110)

    async def __call__(self, query, kind):
        hint = {"smalltalk": "They greeted you or asked about you. Greet them back and name what you can help with.",
                "unsupported": "Answer their question briefly from general knowledge, then say this is not covered by the organisation's documents and name the kinds of topics that are."}[kind]
        # format-forced JSON — qwen3 otherwise narrates chain-of-thought into
        # `content` even with think:false; a schema makes it emit the reply only
        output = await self.handler(
            {"model": self.model, "think": False, "stream": False, "keep_alive": "10m",
             "format": {"type": "object", "additionalProperties": False,
                        "required": ["reply"],
                        "properties": {"reply": {"type": "string", "maxLength": 1200}}},
             "messages": [{"role": "system", "content": ASSISTANT_SYSTEM},
                          {"role": "user", "content": f"{hint}\n\nThey said: {query}"}],
             "options": {"temperature": 0.3, "num_predict": 240, "num_ctx": 4096}},
            "assistant")
        text = (output.get("message") or {}).get("content") or ""
        if not output.get("done"):
            raise ServiceError("assistant_unavailable")
        try:
            reply = json.loads(text).get("reply", "")
        except json.JSONDecodeError:
            reply = text
        if not reply.strip():
            raise ServiceError("assistant_unavailable")
        return reply.strip()


ANSWERER_SYSTEM = """You rewrite retrieved source text into a direct answer.
Rules:
- Use ONLY the source sentences provided — never add facts, dates, names or steps.
- Answer the question directly in 1-3 plain sentences; no lists, no headings.
- If the sources do not contain the answer, output exactly NOT_COVERED."""


class OllamaAnswerer:
    """LLM answer generation for domain skills — model writes fluent prose from
    the retrieved chunks; citations/evidence are untouched (still code-computed),
    so verification semantics are unchanged. Any failure or NOT_COVERED falls back
    to the extractive sentence selection."""

    def __init__(self, model=None, handler=None):
        self.model = model or os.environ.get("CORTEX_ANSWER_MODEL", "qwen3:4b")
        self.handler = handler or HTTPHandler("http://127.0.0.1:11434/api/chat", "local", timeout=110)

    async def __call__(self, query, sources):
        output = await self.handler(
            {"model": self.model, "think": False, "stream": False, "keep_alive": "10m",
             "format": {"type": "object", "additionalProperties": False,
                        "required": ["answer"],
                        "properties": {"answer": {"type": "string", "maxLength": 1200}}},
             "messages": [{"role": "system", "content": ANSWERER_SYSTEM},
                          {"role": "user", "content": "QUESTION: " + query + "\n\nSOURCES:\n" + "\n---\n".join(sources)}],
             "options": {"temperature": 0.2, "num_predict": 300, "num_ctx": 4096}},
            "answerer")
        text = (output.get("message") or {}).get("content") or ""
        if not output.get("done"):
            raise ServiceError("answerer_unavailable")
        try:
            answer = json.loads(text).get("answer", "")
        except json.JSONDecodeError:
            answer = text
        if not answer.strip() or "NOT_COVERED" in answer:
            raise ServiceError("answerer_unavailable")
        return answer.strip()


def local_pipeline(corpus, mock=False, merger=None, verifier=None, answerer=None):
    """Domain skills from a CorpusIndex plus local merge/verify.

    merger/verifier default to the deterministic pair; pass OllamaMerger() /
    OllamaVerifier() for the LLM variants (each falls back deterministic on failure).
    answerer (e.g. OllamaAnswerer) rewrites extractive text into prose per skill.
    Manav's remote M2/V1 services still replace this pair in live mode."""
    services = corpus.services(mock=mock, answerer=answerer)
    services["m2"] = Service("m2", "local", merger or _merge_handler, resource_group="corpus", mock=mock)
    services["v1"] = Service("v1", "local", verifier or _verify_handler, resource_group="corpus", mock=mock)
    return services


def demo_services(corpus=None, merger=None, verifier=None, answerer=None):
    """Seed-corpus-backed demo pipeline. Answers are real extractive retrieval over the
    seed documents, still labelled demo because the corpus is synthetic starter data."""
    from store import CorpusIndex, load_documents
    corpus = corpus or CorpusIndex(load_documents())
    return local_pipeline(corpus, mock=True, merger=merger, verifier=verifier, answerer=answerer)


def load_services(path):
    with Path(path).open(encoding="utf-8") as handle:
        config = json.load(handle, object_pairs_hook=unique_object)
    if not isinstance(config, dict) or "services" not in config or not set(config) <= {"services", "domain_metadata"}:
        raise ValueError("service config must have 'services' and optional 'domain_metadata' only")
    if not isinstance(config["services"], list) or len(config["services"]) > len(DOMAINS) + 2:
        raise ValueError("Invalid service list")
    registry, seen = {}, set()
    for item in config["services"]:
        require_keys(item, ("name", "url", "location", "enabled", "resource_group", "token_env"), "service config entry")
        name = item["name"]
        if name not in (*DOMAINS, "m2", "v1") or name in seen or type(item["enabled"]) is not bool:
            raise ValueError("Invalid or duplicate service name")
        if not isinstance(item["resource_group"], str):
            raise ValueError("Invalid resource group setting")
        seen.add(name)
        if item["enabled"]:
            registry[name] = Service(name, item["location"], HTTPHandler(item["url"], item["location"], item["token_env"]), item["resource_group"])
    return registry


def load_domain_metadata(path):
    """Optional domain titles/keywords for the confidence scorer in live mode."""
    with Path(path).open(encoding="utf-8") as handle:
        config = json.load(handle, object_pairs_hook=unique_object)
    metadata = config.get("domain_metadata") or {}
    titles, keywords = {}, {}
    for domain, body in metadata.items():
        if domain not in DOMAINS or not isinstance(body, dict):
            raise ValueError("Invalid domain metadata")
        if "title" in body:
            titles[domain] = body["title"]
        if "keywords" in body:
            keywords[domain] = list(body["keywords"])
    return titles, keywords
