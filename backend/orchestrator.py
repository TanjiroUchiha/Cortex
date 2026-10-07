import asyncio
import json
import os
import math
import time
import uuid
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Awaitable, Callable

from backend import obs
from models.m1 import (DOMAINS, Request, SMALLTALK_MESSAGES, ambiguous_item_options,
                parse_decision, require_keys, require_text)


class ServiceError(Exception):
    def __init__(self, code, retryable=False, retry_after=0):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass(frozen=True)
class Service:
    name: str
    location: str
    handler: Callable[[dict, str], Awaitable[dict]]
    resource_group: str = ""
    mock: bool = False

    def __post_init__(self):
        if self.name not in (*DOMAINS, "m2", "v1") or self.location not in ("local", "cloud"):
            raise ValueError("Invalid service metadata")


def validate_result(name, value):
    if name == "v1":
        require_keys(value, ("status", "flags", "explanation"), "verification result")
        if value["status"] not in ("passed", "failed", "uncertain"):
            raise ValueError("Invalid verification status")
        if not isinstance(value["flags"], list) or len(value["flags"]) > 20:
            raise ValueError("Invalid verification flags")
        for flag in value["flags"]:
            require_text(flag, "flag", 200)
        require_text(value["explanation"], "explanation", 2000, allow_empty=True)
    elif name == "m2":
        require_keys(value, ("response", "citations"), "merged result")
        require_text(value["response"], "response", 20000)
        _citations(value["citations"])
    else:
        if not isinstance(value, dict) or not {"answer", "citations"} <= set(value) or not set(value) <= {"answer", "citations", "evidence"}:
            raise ValueError("domain skill result must have answer, citations and optional evidence only")
        require_text(value["answer"], "answer", 20000)
        _citations(value["citations"])
        if not value["citations"]:
            raise ValueError("A grounded answer requires at least one citation")
        if "evidence" in value:
            evidence = value["evidence"]
            if not isinstance(evidence, list) or len(evidence) > 10:
                raise ValueError("Invalid evidence list")
            for item in evidence:
                require_keys(item, ("doc_id", "chunk"), "evidence chunk")
                require_text(item["doc_id"], "doc_id", 100)
                require_text(item["chunk"], "chunk", 4000)
    return value


def _citations(citations):
    if not isinstance(citations, list) or len(citations) > len(DOMAINS) * 10:
        raise ValueError("Invalid citations list")
    for item in citations:
        require_keys(item, ("doc_id", "title"), "citation")
        require_text(item["doc_id"], "doc_id", 100)
        require_text(item["title"], "title", 200)


def merge_answers(domain_answers):
    """Deterministic local merger: per-domain sections, citations preserved."""
    sections, citations, seen = [], [], set()
    for item in domain_answers:
        label = item["domain"].upper()
        sections.append(f"[{label}] {item['answer']}")
        for citation in item["citations"]:
            if citation["doc_id"] not in seen:
                seen.add(citation["doc_id"])
                citations.append(citation)
    return {"response": "\n\n".join(sections), "citations": citations}


def verify_grounding(domain_answers, draft, citations):
    """Deterministic local verifier: every citation must trace to retrieved evidence."""
    evidence_ids, flags = set(), []
    for item in domain_answers:
        for chunk in item.get("evidence", []):
            evidence_ids.add(chunk["doc_id"])
        for citation in item["citations"]:
            evidence_ids.add(citation["doc_id"])
    for citation in citations:
        if citation["doc_id"] not in evidence_ids:
            flags.append(f"ungrounded_citation:{citation['doc_id']}")
    if not draft.strip():
        flags.append("empty_draft")
    status = "passed" if not flags else ("failed" if any(f.startswith("ungrounded") for f in flags) else "uncertain")
    return {"status": status, "flags": flags,
            "explanation": "Deterministic citation-vs-evidence check" if not flags else "Some citations lack retrieved evidence"}


class Orchestrator:
    def __init__(self, router, services, call_timeout=30, total_timeout=120, retry_delay=0.25,
                 attempts=2, max_requests=2, scorer=None, confidence_threshold=0.15,
                 clarify_limit=1, domain_titles=None, metrics_path=None,
                 assistant=None, contacts=None, helpdesk=None):
        if any(name != service.name for name, service in services.items()):
            raise ValueError("Service registry keys must match names")
        if not 1 <= attempts <= 3 or call_timeout <= 0 or total_timeout <= 0 or max_requests < 1:
            raise ValueError("Invalid execution limits")
        if not 0 <= confidence_threshold <= 1 or clarify_limit < 0:
            raise ValueError("Invalid confidence or clarify settings")
        self.router = router
        self.services = dict(services)
        self.call_timeout = call_timeout
        self.total_timeout = total_timeout
        self.retry_delay = retry_delay
        self.attempts = attempts
        self.requests = asyncio.Semaphore(max_requests)
        self.dispatch = asyncio.Semaphore(int(os.getenv("CORTEX_MAX_DISPATCH", "5")))
        self.resources = {"shared-local-models": asyncio.Semaphore(1)}
        for service in services.values():
            self.resources.setdefault(self.group(service), asyncio.Semaphore(1))
        self.scorer = scorer
        self.confidence_threshold = confidence_threshold
        self.clarify_limit = clarify_limit
        self.domain_titles = domain_titles or {}
        self.assistant = assistant            # optional LLM conversational fallback
        self.contacts = contacts or {}        # domain -> "Name — email (ext)" escalation line
        self.helpdesk = helpdesk or ""        # generic fallback contact
        self.metrics = Counter()
        self.feedback = {}
        self.seen_requests = set()
        self.metrics_path = Path(metrics_path) if metrics_path else None
        self.queries_path = None    # JSONL request log — set by the app owner (api.py)
        self.tickets_path = None    # JSON list of handoff records — same
        self._load_metrics()

    def _load_metrics(self):
        """Warm counters/feedback from disk so restarts keep history; corrupt files start fresh."""
        if not self.metrics_path:
            return
        try:
            saved = json.loads(self.metrics_path.read_text(encoding="utf-8"))
            self.metrics.update(saved.get("counters", {}))
            self.feedback.update({k: bool(v) for k, v in saved.get("feedback", {}).items()})
        except (OSError, ValueError, TypeError):
            pass

    def _save_metrics(self):
        """Atomic small JSON write; best-effort — metrics must never break a request."""
        if not self.metrics_path:
            return
        try:
            tmp = self.metrics_path.with_suffix(self.metrics_path.suffix + ".tmp")
            tmp.write_text(json.dumps({"counters": dict(self.metrics), "feedback": self.feedback}),
                           encoding="utf-8")
            tmp.replace(self.metrics_path)
        except OSError:
            pass

    @staticmethod
    def group(service):
        return service.resource_group or ("shared-local-models" if service.location == "local" else service.name)

    def allowed(self, name, request):
        service = self.services.get(name)
        return service is not None and (request.privacy == "cloud_allowed" or service.location == "local")

    async def invoke(self, name, request, payload, request_id):
        if not self.allowed(name, request):
            raise ServiceError("unavailable_or_policy_blocked")
        service = self.services[name]
        async with self.dispatch, self.resources[self.group(service)]:
            for attempt in range(self.attempts):
                self.metrics[f"{name}.attempts"] += 1
                try:
                    output = await asyncio.wait_for(service.handler(payload, f"{request_id}:{name}"), self.call_timeout)
                    return validate_result(name, output)
                except asyncio.TimeoutError:
                    error = ServiceError("timeout", retryable=True)
                except ServiceError as exc:
                    error = exc
                except (ValueError, TypeError, KeyError):
                    error = ServiceError("invalid_service_response")
                except Exception:
                    error = ServiceError("service_error")
                if not error.retryable or attempt + 1 == self.attempts:
                    self.metrics[f"{name}.failures"] += 1
                    raise error
                await asyncio.sleep(min(2, max(self.retry_delay * 2 ** attempt, error.retry_after)))

    def record_feedback(self, request_id, resolved):
        known = request_id in self.seen_requests
        self.feedback[request_id] = bool(resolved)
        self.metrics["feedback.resolved" if resolved else "feedback.unresolved"] += 1
        self._save_metrics()
        return known

    async def run(self, request: Request, on_event=None):
        """on_event: optional sync callback receiving stage-telemetry dicts
        ({"event": "stage"|"skill", ...}) as the pipeline progresses — the SSE
        endpoint streams them; they never change the result."""
        obs.ensure_request()          # HTTP requests arrive scoped; direct callers get one here
        request_id = obs.request_id()
        obs.log_query(request.query, available=list(request.available),
                      clarify_attempts=request.clarify_attempts)
        result = {"request_id": request_id, "status": "started", "response": None, "draft": None,
                  "routing": None, "message": None, "skills": [], "citations": [], "errors": [],
                  "verification": {"status": "not_run"}, "mock_services": [], "elapsed_ms": 0}
        if self.requests.locked():
            result["status"] = "busy"
            return result
        started = time.monotonic()
        self.metrics["requests"] += 1
        async with self.requests:
            try:
                await asyncio.wait_for(self._run(request, result, on_event), self.total_timeout)
            except asyncio.TimeoutError:
                result.update(status="deadline_exceeded", response=None)
                result["errors"].append({"service": "m1", "code": "deadline_exceeded"})
        self.seen_requests.add(request_id)
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
        self.metrics[result["status"]] += 1
        self._save_metrics()
        self._record_request(request, result)
        obs.pipeline_summary(result["status"], result["elapsed_ms"],
                             skills=len(result["skills"]), citations=len(result["citations"]),
                             errors=len(result["errors"]),
                             flags=(result.get("v1_verdict") or {}).get("flags", []))
        obs.pipeline_trace()
        return result

    def _record_request(self, request, result):
        """Per-request JSONL line (real usage → labeled routing data) + a ticket
        record on handoff so the fallback is actionable. Best-effort — logging
        must never break a response."""
        if self.queries_path:
            try:
                rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "request_id": result["request_id"], "query": request.query,
                       "status": result["status"],
                       "domains": (result.get("routing") or {}).get("domains", []),
                       "elapsed_ms": result["elapsed_ms"]}
                with open(self.queries_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError:
                pass
        if self.tickets_path and result["status"] == "handoff":
            try:
                tickets = json.loads(self.tickets_path.read_text(encoding="utf-8")) \
                    if self.tickets_path.exists() else []
                ticket = {"id": f"CTX-{len(tickets) + 1:04d}",
                          "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                          "request_id": result["request_id"], "query": request.query,
                          "clarify_attempts": request.clarify_attempts,
                          "options": [o.get("domain") for o in (result.get("routing") or {}).get("options", [])]}
                tickets.append(ticket)
                tmp = self.tickets_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(tickets, indent=1, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self.tickets_path)
                result["ticket"] = ticket["id"]
            except (OSError, ValueError):
                pass

    async def _assistant_reply(self, query, kind):
        """Optional LLM conversational reply for chit-chat/off-corpus turns.
        Shares the local-model semaphore (it competes with the router on the
        same Ollama box) and fails open to the deterministic message."""
        if self.assistant is None:
            return None
        try:
            async with self.resources["shared-local-models"]:
                return await asyncio.wait_for(self.assistant(query, kind), timeout=100)
        except Exception:
            return None

    async def _confidence_gate(self, effective, decision, result):
        """Deterministic confidence: share of keyword evidence per routed domain."""
        if not self.scorer or decision["action"] != "route":
            return decision, None
        if decision.get("evidence_resolved"):
            # the router's retrieval probe already adjudicated this domain —
            # document-level evidence beats the keyword-share heuristic
            return decision, None
        # a semantic scorer embeds via a synchronous HTTP call — keep it off the loop
        scores = await asyncio.to_thread(self.scorer, effective.query, list(effective.available))
        total = sum(scores.values())
        shares = {d: (scores.get(d, 0) / total if total else 0) for d in effective.available}
        confidence = min(shares.get(t["domain"], 0) for t in decision["tasks"])
        result["routing"]["confidence"] = round(confidence, 3)
        if confidence >= self.confidence_threshold:
            return decision, None
        ranked = sorted(((s, d) for d, s in scores.items()), reverse=True)
        options = [{"domain": d, "title": self.domain_titles.get(d, d)} for s, d in ranked[:3] if s > 0]
        if not options:
            options = [{"domain": d, "title": self.domain_titles.get(d, d)} for d in effective.available[:3]]
        return None, options

    async def _run(self, request, result, on_event=None):
        def emit(event, **fields):
            if on_event is None:
                return
            try:
                on_event({"event": event, **fields})
            except Exception:
                pass  # stage telemetry must never break the pipeline
        eligible = tuple(name for name in request.available if self.allowed(name, request))
        effective = replace(request, available=eligible)
        if not eligible:
            result.update(status="unsupported",
                          routing={"action": "unsupported", "domains": [], "message": "No knowledge domains are permitted and configured."})
            result["message"] = result["routing"]["message"]
            emit("stage", stage="m1", state="done", action="unsupported", domains=[])
            return
        emit("stage", stage="m1", state="active")
        with obs.stage("M1", model=getattr(self.router, "model", "deterministic"),
                       input_chars=len(effective.query), scope=len(effective.available)) as m1_out:
            try:
                async with self.resources["shared-local-models"]:
                    decision = await asyncio.wait_for(self.router(effective), self.call_timeout)
                resolved = decision.pop("evidence_resolved", False)
                decision = parse_decision(json.dumps(decision), effective, options_pool=request.available)
                if resolved:
                    decision["evidence_resolved"] = True
            except Exception:
                m1_out["state"] = "fail"
                emit("stage", stage="m1", state="fail")
                result.update(status="routing_failed")
                result["errors"].append({"service": "m1", "code": "invalid_or_unavailable_router"})
                obs.log_decision("REJECT", reason="router_failed")
                return
            m1_out.update(action=decision["action"],
                          domains=[t["domain"] for t in decision["tasks"]],
                          options=len(decision["options"]))
        result["routing"] = {"action": decision["action"],
                             "domains": [t["domain"] for t in decision["tasks"]],
                             "message": decision["message"],
                             "options": [{"domain": o["domain"], "title": self.domain_titles.get(o["domain"], o["domain"])}
                                         for o in decision["options"]],
                             "confidence": None}
        # smalltalk is conversational, not an ambiguity failure — a real reply when
        # a model is up, and it never burns a clarify_attempt toward handoff
        smalltalk = (decision["action"] == "clarify"
                     and decision.get("message") in SMALLTALK_MESSAGES.values())
        if smalltalk:
            reply = await self._assistant_reply(effective.query, "smalltalk")
            if reply:
                decision["message"] = reply
                result["routing"]["message"] = reply
        if decision["action"] == "clarify" and not smalltalk and effective.clarify_attempts >= self.clarify_limit:
            decision = {"action": "handoff", "tasks": [],
                        "message": "I could not pin down the right department. Please contact the help desk directly.",
                        "options": decision["options"]}
            if self.helpdesk:
                decision["message"] += f" {self.helpdesk}."
            result["routing"]["action"] = "handoff"
            result["routing"]["message"] = decision["message"]
        if decision["action"] == "route":
            # a single-domain scope IS the user's clarification — re-flagging
            # the same ambiguous term would loop clarify → instant handoff
            # an evidence probe that singled out ONE domain has already
            # adjudicated the item-word; a probe that found several equally
            # strong domains IS the ambiguity — still ask
            ambiguous = ambiguous_item_options(effective.query, effective.available) \
                if len(effective.available) > 1 \
                and not (decision.get("evidence_resolved") and len(decision["tasks"]) == 1) \
                else None
            if ambiguous is not None:
                # offer the evidence-ranked domains first — the probe already
                # found which candidate holds the stronger document
                rank = {t["domain"]: i for i, t in enumerate(decision.get("tasks", []))}
                ambiguous = sorted(ambiguous, key=lambda o: rank.get(o["domain"], len(rank)))
                action = "handoff" if effective.clarify_attempts >= self.clarify_limit else "clarify"
                message = ("That could fall under a couple of different teams — which best describes it?"
                           if action == "clarify" else
                           "I could not pin down the right department. Please contact the help desk directly.")
                result["routing"].update(action=action, options=ambiguous, message=message)
                result["status"] = action
                result["message"] = message
                obs.log_decision(action.upper(), reason="ambiguous_term",
                                 options=[o["domain"] for o in ambiguous])
                emit("stage", stage="m1", state="done", action=action, domains=[])
                return
            gated, options = await self._confidence_gate(effective, decision, result)
            if gated is None:
                action = "handoff" if effective.clarify_attempts >= self.clarify_limit else "clarify"
                gate_message = ("Which area should I check?" if action == "clarify"
                                else "I could not pin down the right department. Please contact the help desk directly.")
                if action == "handoff" and self.helpdesk:
                    gate_message += f" {self.helpdesk}."
                result["routing"].update(action=action, options=options or [], message=gate_message)
                result["status"] = action
                result["message"] = result["routing"]["message"]
                obs.log_decision(action.upper(), reason="insufficient_evidence",
                                 options=[o["domain"] for o in (options or [])])
                emit("stage", stage="m1", state="done", action=action, domains=[])
                return
            decision = gated
        if decision["action"] != "route":
            reason = "clarify_limit_reached" if decision["action"] == "handoff" else "router_decision"
            obs.log_decision(decision["action"].upper(), reason=reason,
                             clarify_attempts=effective.clarify_attempts)
            emit("stage", stage="m1", state="done", action=decision["action"], domains=[])
            if decision["action"] == "unsupported":
                reply = await self._assistant_reply(effective.query, "unsupported")
                message = reply or decision["message"]
                if self.helpdesk:
                    message += f" For anything I cannot answer: {self.helpdesk}."
                decision["message"] = message
                result["routing"]["message"] = message
            result["status"] = decision["action"]
            result["message"] = decision["message"]
            return
        emit("stage", stage="m1", state="done", action="route",
             domains=[t["domain"] for t in decision["tasks"]])
        for task in decision["tasks"]:
            self.metrics[f"route.{task['domain']}"] += 1   # per-domain routing mix in /metrics
        emit("stage", stage="skills", state="active",
             domains=[t["domain"] for t in decision["tasks"]])
        base_payload = {"request_id": result["request_id"], "request": effective.to_dict()}

        async def skill(task):
            name = task["domain"]
            # multi-topic query: each domain only covers its own slice of the
            # query terms, so the whole-query coverage floor inside retrieve is
            # structurally unsatisfiable here — tell the skill to skip it (the
            # classifier's probe/keyword gates already vetted the evidence).
            payload = {**base_payload, "domain": name, "instruction": task["instruction"],
                       "multi_topic": len(decision.get("tasks") or []) > 1}
            try:
                output = await self.invoke(name, effective, payload, result["request_id"])
                if self.services[name].mock:
                    result["mock_services"].append(name)
                emit("skill", domain=name, state="done",
                     citations=len(output["citations"]), evidence=len(output.get("evidence", [])))
                return {"domain": name, "location": self.services[name].location, **output}
            except ServiceError as exc:
                emit("skill", domain=name,
                     state="abstained" if exc.code == "no_evidence" else "fail", code=exc.code)
                result["errors"].append({"service": name, "code": exc.code})
                return None

        outputs = await asyncio.gather(*(skill(task) for task in decision["tasks"]))
        result["skills"] = [output for output in outputs if output is not None]
        emit("stage", stage="skills", state="done" if result["skills"] else "fail",
             answered=len(result["skills"]), failed=len(result["errors"]))
        if not result["skills"]:
            obs.log_decision("STOP", reason="all_skills_failed",
                             codes=[e["code"] for e in result["errors"]])
            all_missing = all(e["code"] == "no_evidence" for e in result["errors"])
            message = None
            if all_missing:
                missing = [e["service"] for e in result["errors"] if e["code"] == "no_evidence"]
                contact = self.contacts.get(missing[0]) if len(missing) == 1 else self.helpdesk
                message = "I could not find this in the configured knowledge bases."
                if contact:
                    message += f" {contact} can help directly."
            result.update(status="no_evidence" if all_missing else "failed", message=message)
            return
        payload = {**base_payload, "domain_answers": result["skills"], "failures": result["errors"]}
        emit("stage", stage="m2", state="active")
        with obs.stage("M2", service=self.services["m2"].location,
                       domain_answers=len(result["skills"]), failures=len(result["errors"])) as m2_out:
            try:
                merged = await self.invoke("m2", effective, payload, result["request_id"])
            except ServiceError as exc:
                m2_out["state"] = "fail"
                emit("stage", stage="m2", state="fail", code=exc.code)
                result["status"] = "aggregation_unavailable"
                result["errors"].append({"service": "m2", "code": exc.code})
                obs.log_decision("REJECT", reason="m2_failed", code=exc.code)
                return
            m2_out.update(response_chars=len(merged["response"]), citations=len(merged["citations"]))
        emit("stage", stage="m2", state="done", citations=len(merged["citations"]))
        if self.services["m2"].mock:
            result["mock_services"].append("m2")
        result["draft"] = merged["response"]
        result["citations"] = merged["citations"]
        emit("stage", stage="v1", state="active")
        with obs.stage("V1", service=self.services["v1"].location,
                       citations=len(merged["citations"]),
                       draft_chars=len(merged["response"])) as v1_out:
            try:
                verification = await self.invoke("v1", effective, {**payload, "response": merged["response"], "citations": merged["citations"]}, result["request_id"])
            except ServiceError as exc:
                v1_out["state"] = "fail"
                emit("stage", stage="v1", state="fail", code=exc.code)
                result.update(status="unverified", verification={"status": "unavailable"})
                result["errors"].append({"service": "v1", "code": exc.code})
                obs.log_decision("REJECT", reason="v1_unavailable", code=exc.code)
                return
            v1_out.update(verdict=verification["status"], flags=verification.get("flags", []))
        emit("stage", stage="v1", state="done", status=verification["status"])
        obs.log_flags(verification.get("flags", []), verdict=verification["status"],
                      explanation=verification.get("explanation", ""))
        result["v1_verdict"] = verification
        if self.services["v1"].mock:
            result["mock_services"].append("v1")
        if result["mock_services"]:
            verification = {"status": "mock", "flags": ["not_real_verification"], "explanation": "Demo services were used; no verified answer is claimed."}
            result["status"] = "demo"
        elif verification["status"] == "passed":
            result.update(status="partial" if result["errors"] else "completed", response=merged["response"])
        else:
            result["status"] = "needs_review"
        obs.log_decision(result["status"].upper(), reason=f"v1:{verification['status']}",
                         flags=verification.get("flags", []))
        result["verification"] = verification
