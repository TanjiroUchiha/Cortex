import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone
from backend import obs
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request as HTTPRequest, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend import auth
from backend.access import can_view
from backend.auth import (AuthDB, Principal, RateLimiter, get_principal, issue_jwt,
                          load_jwt_secret, require_user, require_role)
from models.m1 import DOMAINS, Request, keyword_scores
from backend.orchestrator import Orchestrator
from backend.services import (CheckVerifier, KeywordRouter, OllamaAnswerer, OllamaAssistant, OllamaMerger,
                      OllamaRouter, OllamaVerifier, RemoteMerger, demo_services,
                      load_domain_metadata, load_services)


class QueryBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=8000)
    privacy: Literal["local_only", "cloud_allowed"] = "local_only"
    available: list[str] = Field(default_factory=lambda: list(DOMAINS), max_length=len(DOMAINS))
    clarify_attempts: int = Field(default=0, ge=0, le=3)


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=100)
    resolved: bool


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class SignupBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=256)
    name: str = Field(default="", max_length=120)


class CreateUserBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=256)
    name: str = Field(default="", max_length=120)
    role: Literal["user", "admin"] = "user"


class UpdateUserBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["user", "admin"] | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=10, max_length=256)


class ConversationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: str = Field(min_length=1, max_length=80)
    title: str | None = Field(default=None, max_length=200)
    draft: str = Field(default="", max_length=4000)
    data: dict


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        parts, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > 256000:
                response = JSONResponse({"detail": "Request body too large"}, status_code=413)
                return await response(scope, receive, send)
            parts.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        consumed = False
        async def bounded_receive():
            nonlocal consumed
            if consumed:
                return await receive()
            consumed = True
            return {"type": "http.request", "body": b"".join(parts), "more_body": False}
        await self.app(scope, bounded_receive, send)


def _demo_engine(retrieval="semantic", llm_merge=False, llm_verify=False, llm_route=False,
                 m1_backcheck=False, llm_answer=False, m2_url=None):
    from backend.store import CorpusIndex, SemanticIndex, load_documents
    corpus_path = Path(__file__).resolve().parent.parent / "dataset" / "corpus.json"
    documents = load_documents(corpus_path)
    corpus = None
    if retrieval == "semantic":
        try:
            corpus = SemanticIndex(documents)   # embeds the corpus; needs Ollama + embedding model
        except Exception:
            corpus = None
    ollama_up = isinstance(corpus, SemanticIndex)
    if corpus is None:
        corpus = CorpusIndex(documents)         # keyword/IDF fallback — no model needed
        if llm_merge or llm_verify or llm_answer:
            try:
                from backend.store import OllamaEmbedder
                OllamaEmbedder()("probe")       # keyword retrieval can still get the LLM helpers
                ollama_up = True
            except Exception:
                ollama_up = False
        elif llm_route:
            try:
                import urllib.request
                urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3).close()
                ollama_up = True                # chat model lives behind the same daemon
            except Exception:
                ollama_up = False
    merge_model_available = False
    try:
        import urllib.request
        tags = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:11434/api/tags", timeout=3
        ).read())
        merge_model = os.environ.get("CORTEX_MERGE_MODEL", "qwen3:4b")
        merge_model_available = any(
            model.get("name") == merge_model
            or (":" not in merge_model
                and model.get("name", "").split(":")[0] == merge_model)
            for model in tags.get("models", [])
        )
    except Exception:
        merge_model_available = False
    merger = RemoteMerger(m2_url) if m2_url else (
        OllamaMerger() if (llm_merge or merge_model_available) else None
    )
    verifier = CheckVerifier(OllamaVerifier() if llm_verify and ollama_up else None)
    answerer = OllamaAnswerer() if llm_answer and ollama_up else None
    if llm_answer and not ollama_up:
        print("note: --llm-answer needs Ollama; domain skills stay extractive")
    if llm_route and not ollama_up:
        print("note: --llm-route needs Ollama; falling back to the keyword router")
    router = OllamaRouter(backcheck=m1_backcheck) if llm_route and ollama_up else KeywordRouter(corpus)
    # Conversational fallback for chit-chat/off-corpus turns — enabled whenever the
    # chat model is actually pulled, regardless of which router is in use.
    assistant = None
    if os.environ.get("CORTEX_ASSISTANT", "on") != "off":
        try:
            import urllib.request
            tags = json.loads(urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3).read())
            want = os.environ.get("CORTEX_M1_MODEL", "qwen3:4b")
            if any(m.get("name") == want or m.get("name", "").split(":")[0] == want.split(":")[0]
                   for m in tags.get("models", [])):
                assistant = OllamaAssistant()
        except Exception:
            assistant = None
    contacts = {d: b["contact"] for d, b in corpus.documents.items() if b.get("contact")}
    services = demo_services(corpus, merger=merger, verifier=verifier, answerer=answerer)
    engine = Orchestrator(router, services, scorer=corpus.scores,
                          domain_titles=corpus.titles(),
                          assistant=assistant, contacts=contacts,
                          helpdesk=contacts.get("general", ""),
                          call_timeout=120 if (merger or llm_route or answerer) else 30)  # local LLM calls need a bigger budget on CPU
    return engine, corpus


def _live_engine(service_config):
    registry = load_services(service_config)
    if not registry:
        raise ValueError("Live mode has no enabled services")
    titles, keywords = load_domain_metadata(service_config)
    scorer = (lambda query, domains: keyword_scores(query, {d: keywords.get(d, []) for d in domains})) if keywords else None
    return Orchestrator(OllamaRouter(), registry, scorer=scorer, domain_titles=titles,
                        assistant=OllamaAssistant())


def make_app(engine=None, mode="demo", service_config=None, api_token=None, retrieval="semantic",
             llm_merge=False, llm_verify=False, llm_route=False, m1_backcheck=False, llm_answer=False, m2_url=None,
             corpus=None, metrics_path=None, persist_root=None,
             queries_path=None, tickets_path=None, allowed_origins=None, auth_db=None):
    if mode not in ("demo", "live"):
        raise ValueError("Choose demo or live mode explicitly")
    if engine is None:
        if mode == "live" and not service_config:
            raise ValueError("Live mode requires a configured services file")
        if mode == "demo":
            engine, corpus = _demo_engine(retrieval, llm_merge, llm_verify, llm_route, m1_backcheck, llm_answer, m2_url)
        else:
            engine = _live_engine(service_config)
    if metrics_path:
        engine.metrics_path = Path(metrics_path)
        engine._load_metrics()
    if queries_path:
        engine.queries_path = Path(queries_path)
    if tickets_path:
        engine.tickets_path = Path(tickets_path)
    if mode == "live" and any(service.mock for service in engine.services.values()):
        raise ValueError("Live mode must not contain mock services")
    app = FastAPI(title="Cortex — One Front Door", version="0.2.0",
                description="Domain-routed RAG assistant. Demo mode uses the seed corpus and deterministic local merge/verify.")

    # ── auth wiring ──────────────────────────────────────────────────────────
    # MongoDB Atlas account store (backend/db.py); JWT bearer tokens; role
    # re-resolved from the collections per request so deactivation is immediate
    # and the token's role claim is cosmetic. Tests inject AuthDB.memory().
    auth_db = auth_db or AuthDB.for_env()
    app.state.auth_db = auth_db
    app.state.documents = auth_db.db["documents"]
    app.state.jwt_secret = load_jwt_secret(mode)
    app.state.api_token = api_token          # service token → synthetic admin principal
    app.state.login_limiter = RateLimiter(limit=8, window=60.0)
    app.state.guest_query_limiter = RateLimiter(limit=10, window=3600.0)  # per-IP, per hour
    if not auth_db.has_admin():
        email = os.environ.get("CORTEX_ADMIN_EMAIL", "").strip()
        password = os.environ.get("CORTEX_ADMIN_PASSWORD", "")
        if email and password:
            auth_db.create(email, name="Admin", password=password, role="admin")
            print(f"auth: bootstrap admin created for {email}")
        else:
            password = secrets.token_urlsafe(12)
            auth_db.create("admin@cortex.local", name="Admin", password=password, role="admin")
            print(f"auth: no admin existed — created admin@cortex.local with password: {password}")
            print("      (dev bootstrap; set CORTEX_ADMIN_EMAIL/CORTEX_ADMIN_PASSWORD to control this)")

    app.add_middleware(BodyLimit)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])
    obs.configure()

    @app.middleware("http")
    async def observability(request: HTTPRequest, call_next):
        """Request-scoping: mints the request_id every event shares, times the
        whole call, and always emits REQUEST_END — even on unhandled errors.
        (On SSE routes this wraps the handler's return, not the stream drain.)"""
        token = obs.begin_request()
        obs.event("REQUEST_START", method=request.method, path=request.url.path,
                  client=request.client.host if request.client else "?")
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        except Exception as exc:
            obs.event("REQUEST_ERROR", level=logging.ERROR, exc=exc, path=request.url.path)
            raise
        finally:
            total = round((time.perf_counter() - started) * 1000, 1)
            obs.event("RESPONSE_READY", http_status=status, total_ms=total)
            obs.event("REQUEST_END", http_status=status, total_ms=total)
            obs.end_request(token)

    # Cross-origin is off unless explicitly configured. The UI is normally served by
    # this same app (static mount below), so no CORS is needed; set CORTEX_ALLOWED_ORIGINS
    # (or --allow-origin) to also accept a separately hosted client, e.g. the VS Code
    # Live Server on http://127.0.0.1:5500. ORIGIN allowlist only — never a wildcard.
    origins = list(dict.fromkeys(o.strip() for o in (allowed_origins or []) if o and o.strip()))
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False,
                           allow_methods=["GET", "POST", "OPTIONS"],
                           allow_headers=["Content-Type", "Authorization"], max_age=600)

    warmable = engine.router if isinstance(engine.router, OllamaRouter) else getattr(engine, "assistant", None)
    if warmable is not None:
        @app.on_event("startup")
        async def _warm_router():
            async def warm():
                try:
                    await warmable.handler({"model": warmable.model, "think": False,
                                            "stream": False, "keep_alive": "10m",
                                            "messages": [{"role": "user", "content": "ready"}],
                                            "options": {"num_predict": 1, "num_ctx": 64}}, "warmup")
                except Exception:
                    pass
            asyncio.create_task(warm())  # cold-load the chat model in the background; first query shouldn't wait on it

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"detail": [{"loc": error["loc"], "type": error["type"]} for error in exc.errors()]}, status_code=422)

    async def check_origin(request):
        """Same-origin gate, unchanged: cross-origin POSTs/GETs are rejected unless
        the origin is allowlisted. Identity is a separate concern — FastAPI
        dependencies (require_user / require_role) resolve the JWT or the
        CORTEX_API_TOKEN service bearer into a Principal."""
        origin = request.headers.get("origin")
        if origin and origin not in origins and origin != f"{request.url.scheme}://{request.url.netloc}":
            raise HTTPException(403, "Cross-origin requests are disabled")

    # ── auth endpoints ────────────────────────────────────────────────────────

    def client_ip(request) -> str:
        return request.client.host if request.client else "unknown"

    @app.post("/auth/login")
    async def auth_login(body: LoginBody, request: HTTPRequest):
        await check_origin(request)
        limiter = request.app.state.login_limiter
        limiter.check(f"ip:{client_ip(request)}")
        limiter.check(f"email:{body.email.lower()}")
        row = request.app.state.auth_db.get_by_email(body.email)
        if not auth.verify_password(row, body.password):
            raise HTTPException(401, "Incorrect email or password")   # generic — no enumeration
        user = Principal(row["id"], row["email"], row["name"], row["role"], row["auth_provider"])
        return {"token": issue_jwt(user, request.app.state.jwt_secret), "user": user.public()}

    @app.post("/auth/signup", status_code=201)
    async def auth_signup(body: SignupBody, request: HTTPRequest):
        """Self-registration: creates a plain 'user' account and signs in.
        Role is never client-selectable — admins promote via /admin/users."""
        await check_origin(request)
        limiter = request.app.state.login_limiter
        limiter.check(f"ip:{client_ip(request)}")
        limiter.check(f"email:{body.email.lower()}")
        db = request.app.state.auth_db
        if db.get_by_email(body.email):
            raise HTTPException(409, "An account with that email already exists")
        try:
            user = db.create(body.email, name=body.name, password=body.password, role="user")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"token": issue_jwt(user, request.app.state.jwt_secret), "user": user.public()}

    @app.post("/auth/guest")
    async def auth_guest(request: HTTPRequest):
        """'Continue as guest': a 20-minute stateless token, no account. Guests can
        ask questions (per-IP hourly cap enforced at /query) but can't give
        feedback, upload, or see anything admin-only."""
        await check_origin(request)
        request.app.state.login_limiter.check(f"guest-issue:{client_ip(request)}")
        return {"token": auth.issue_guest_jwt(request.app.state.jwt_secret),
                "user": {"role": "guest", "name": "Guest", "email": ""}}

    @app.get("/auth/me")
    async def auth_me(request: HTTPRequest, user: Principal = Depends(require_user)):
        await check_origin(request)
        return user.public()

    # ── admin account management ──────────────────────────────────────────────

    @app.get("/admin/users")
    async def admin_users(request: HTTPRequest, admin: Principal = Depends(require_role("admin"))):
        await check_origin(request)
        return {"users": request.app.state.auth_db.list_users()}

    @app.post("/admin/users", status_code=201)
    async def admin_create_user(body: CreateUserBody, request: HTTPRequest,
                                admin: Principal = Depends(require_role("admin"))):
        await check_origin(request)
        if request.app.state.auth_db.get_by_email(body.email):
            raise HTTPException(409, "An account with that email already exists")
        try:
            user = request.app.state.auth_db.create(body.email, name=body.name,
                                                    password=body.password, role=body.role)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"user": user.public()}

    @app.patch("/admin/users/{user_id}")
    async def admin_update_user(user_id: str, body: UpdateUserBody, request: HTTPRequest,
                                admin: Principal = Depends(require_role("admin"))):
        await check_origin(request)
        db = request.app.state.auth_db
        target = db.get_record(user_id)
        if not target:
            raise HTTPException(404, "No such user")
        demoting_self = (body.role is not None and body.role != "admin")
        if user_id == admin.id and (demoting_self or body.is_active is False):
            raise HTTPException(400, "You can't demote or deactivate your own admin account")
        try:
            if body.role is not None:
                db.set_role(user_id, body.role)
            if body.is_active is not None:
                db.set_active(user_id, body.is_active)
            if body.password is not None:
                db.set_password(user_id, body.password)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"user": db.get_record(user_id)}

    # ── per-account conversations ─────────────────────────────────────────────
    # History is scoped by user_id server-side — the browser just syncs. Guests
    # stay ephemeral (localStorage only, nothing persisted).

    @app.get("/conversations")
    async def conversations(request: HTTPRequest, user: Principal = Depends(require_role("user"))):
        await check_origin(request)
        return {"sessions": request.app.state.auth_db.list_conversations(user.id)}

    @app.put("/conversations/{session_id}")
    async def conversation_put(session_id: str, body: ConversationBody, request: HTTPRequest,
                               user: Principal = Depends(require_role("user"))):
        await check_origin(request)
        if session_id != body.session_id:
            raise HTTPException(422, "session_id mismatch")
        request.app.state.auth_db.put_conversation(
            user.id, session_id, body.title, body.draft, json.dumps(body.data, ensure_ascii=False))
        return {"saved": True}

    @app.delete("/conversations/{session_id}")
    async def conversation_delete(session_id: str, request: HTTPRequest,
                                  user: Principal = Depends(require_role("user"))):
        await check_origin(request)
        removed = request.app.state.auth_db.delete_conversation(user.id, session_id)
        return {"deleted": removed}

    # ── existing endpoints ────────────────────────────────────────────────────

    @app.get("/health")
    async def health(request: HTTPRequest):
        await check_origin(request)   # public — run.py health-wait + landing probe
        return {"status": "ok", "mode": mode, "upstream_health_checked": False}

    @app.get("/domains")
    async def domains(request: HTTPRequest, user: Principal = Depends(require_user)):
        await check_origin(request)
        return {"mode": mode,
                "domains": [{"id": s.name, "title": engine.domain_titles.get(s.name, s.name),
                             "location": s.location, "mock": s.mock}
                            for s in engine.services.values() if s.name in DOMAINS]}

    @app.get("/corpus")
    async def corpus_index(request: HTTPRequest):
        """The real knowledge-base contents — the UI's corpus browser renders
        this, there is no bundled document copy. Access-filtered: anonymous
        callers see the guest tier (PUBLIC docs only); signed-in callers see
        what their role may read. Uploads remain admin-gated."""
        await check_origin(request)
        if corpus is None:
            raise HTTPException(503, "No local corpus in live mode")
        principal = await get_principal(request)
        viewer = principal.role if principal else "guest"
        return {"domains": {d: {"title": body["title"],
                                "sources": [{"id": s["doc_id"], "title": s["title"], "content": s["content"],
                                             "format": s.get("format", "json"),
                                             "file": bool(s.get("file")),
                                             "meta": s.get("metadata", {}),
                                             "path": s.get("relative_path")}
                                            for s in body["sources"].values()
                                            if can_view(s.get("metadata", {}), viewer)]}
                            for d, body in corpus.documents.items()}}

    @app.get("/corpus/file/{doc_id}")
    async def corpus_file(doc_id: str, request: HTTPRequest):
        """Download the original uploaded/indexed file backing a corpus doc.
        Access-filtered like /corpus: docs above the caller's tier answer 404 —
        restricted doc IDs are not oracles."""
        await check_origin(request)
        if corpus is None:
            raise HTTPException(503, "No local corpus in live mode")
        principal = await get_principal(request)
        viewer = principal.role if principal else "guest"
        for body in corpus.documents.values():
            source = body["sources"].get(doc_id)
            if source and source.get("file"):
                if not can_view(source.get("metadata", {}), viewer):
                    raise HTTPException(404, "No file recorded for that document")
                path = Path(source["file"])
                if path.is_file():
                    return FileResponse(path, filename=path.name)
                break
        raise HTTPException(404, "No file recorded for that document")

    @app.get("/corpus/search")
    async def corpus_search(q: str, request: HTTPRequest,
                            user: Principal = Depends(require_user)):
        """Read-only document search for any signed-in user — same visibility
        rule as /corpus: hits above the caller's tier are filtered out. Upload
        and delete stay admin-only (POST /corpus/upload, DELETE /corpus/{id})."""
        await check_origin(request)
        if corpus is None:
            raise HTTPException(503, "No local corpus in live mode")
        q = q.strip()
        if not q:
            raise HTTPException(422, "q must not be empty")
        hits = []
        for domain in corpus.documents:
            for h in await asyncio.to_thread(corpus.retrieve, domain, q, 3, user.role):
                hits.append({"doc_id": h["doc_id"], "title": h["title"],
                             "domain": domain, "score": round(h["score"], 3),
                             "snippet": h["chunk"][:240]})
        seen, results = set(), []
        for hit in sorted(hits, key=lambda h: -h["score"]):
            if hit["doc_id"] not in seen:
                seen.add(hit["doc_id"])
                results.append(hit)
        return {"query": q, "results": results[:10]}

    @app.delete("/corpus/{doc_id}")
    async def corpus_delete(doc_id: str, request: HTTPRequest,
                            admin: Principal = Depends(require_role("admin"))):
        """Remove a document from the shared knowledge base — admin only. Drops
        the index entry, the persisted corpus.d file, and the upload record."""
        await check_origin(request)
        if corpus is None:
            raise HTTPException(503, "No local corpus in live mode")
        found_domain, source = None, None
        for domain, body in corpus.documents.items():
            if doc_id in body["sources"]:
                found_domain, source = domain, body["sources"][doc_id]
                break
        if source is None:
            raise HTTPException(404, "No such document")
        corpus.remove_source(found_domain, doc_id)
        if source.get("file"):
            try:
                Path(source["file"]).unlink(missing_ok=True)
            except OSError:
                pass    # index removal already done; file cleanup is best-effort
        request.app.state.documents.delete_one({"doc_id": doc_id})
        return {"deleted": doc_id, "domain": found_domain}

    @app.get("/metrics")
    async def metrics(request: HTTPRequest, admin: Principal = Depends(require_role("admin"))):
        await check_origin(request)
        resolved = sum(1 for value in engine.feedback.values() if value)
        unresolved = sum(1 for value in engine.feedback.values() if not value)
        total = resolved + unresolved
        return {"counters": dict(engine.metrics),
                "feedback": {"resolved": resolved, "unresolved": unresolved,
                             "resolution_rate": resolved / total if total else None},
                "scope": "persisted to data/metrics.json — survives restarts" if engine.metrics_path
                         else "process-local counts; restart clears them"}

    @app.post("/feedback")
    async def feedback(body: FeedbackBody, request: HTTPRequest,
                       user: Principal = Depends(require_role("user"))):
        await check_origin(request)
        known = engine.record_feedback(body.request_id, body.resolved)
        return {"recorded": True, "known_request_id": known,
                "scope": "resolution feedback, in-memory only"}

    @app.get("/models")
    async def models(request: HTTPRequest, admin: Principal = Depends(require_role("admin"))):
        """Which model powers which pipeline role — transparency for 'is this actually AI?'."""
        await check_origin(request)
        m2_handler = engine.services.get("m2")
        m2_handler = m2_handler.handler if m2_handler else None
        if isinstance(m2_handler, OllamaMerger):
            m2_model = os.environ.get("CORTEX_MERGE_MODEL", "qwen3:4b")
        elif isinstance(m2_handler, RemoteMerger) or mode == "live":
            m2_model = "configured M2 service"
        else:
            m2_model = "deterministic grounded synthesis"
        return {"models": {
            "m1_router": os.environ.get("CORTEX_M1_MODEL", "qwen3.5:4b")
                         if mode == "live" or engine.router.__class__.__name__ == "OllamaRouter"
                         else "keyword-classifier (deterministic)",
            "retrieval": os.environ.get("CORTEX_EMBED_MODEL", "qwen3-embedding:0.6b")
                         if retrieval == "semantic" else "keyword-idf",
            "m2_merger": m2_model,
            "v1_verifier": ("checks + " + os.environ.get("CORTEX_V1_MODEL", "qwen3.5:4b")) if llm_verify else "output-checks (grounding + query fit)"},
            "override": "CORTEX_M1_MODEL / CORTEX_EMBED_MODEL / CORTEX_MERGE_MODEL / CORTEX_V1_MODEL"}

    MAX_UPLOAD_BYTES = 10 * 1024 * 1024

    @app.post("/corpus/upload")
    async def corpus_upload(request: HTTPRequest, domain: str = Form(...), file: UploadFile = File(...),
                            admin: Principal = Depends(require_role("admin"))):
        """Drop a real document (.md/.txt/.pdf/.docx) into a domain at runtime —
        extracted, chunked and retrievable immediately; persists under corpus.d/ when
        the server was started with a persist directory. Demo-mode only. Admin only."""
        await check_origin(request)
        if corpus is None:
            raise HTTPException(400, "Corpus upload requires a local corpus (demo mode)")
        if domain not in DOMAINS:
            raise HTTPException(422, "Unknown domain")
        from backend.store import DOCUMENT_EXTENSIONS, document_source, extract_text
        filename = file.filename or "document"
        if Path(filename).suffix.lower() not in DOCUMENT_EXTENSIONS:
            raise HTTPException(415, f"Unsupported file type: {Path(filename).suffix}")
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "File exceeds the 10 MB limit")
        try:
            text = extract_text(filename, content).strip()
            source = document_source(filename, text, domain)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(422, f"Could not parse {filename}: {exc}") from exc
        if not source["content"]:
            raise HTTPException(422, "Document contained no text")
        doc_id = source["id"]
        try:
            await asyncio.to_thread(corpus.add_source, domain, doc_id, source["title"],
                                    source["content"], source["format"],
                                    metadata=source["metadata"])
        except ValueError as exc:
            raise HTTPException(409 if "Duplicate" in str(exc) else 422, str(exc)) from exc
        scope_msg = "in-memory for this process — restart clears it"
        saved_name = None
        if persist_root is not None:   # keep the source file so a restart re-indexes it like any corpus.d doc
            try:
                persist_dir = Path(persist_root) / domain
                persist_dir.mkdir(parents=True, exist_ok=True)
                safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", doc_id).strip("-.") or "doc"
                saved = persist_dir / f"{safe_id}{Path(filename).suffix.lower()}"
                saved.write_bytes(content)
                corpus.documents[domain]["sources"][doc_id]["file"] = str(saved)
                saved_name = saved.name
                scope_msg = f"saved to data/corpus.d/{domain}/ — survives restarts"
            except OSError:
                scope_msg = "in-memory for this process — could not persist the file"
        request.app.state.documents.update_one(
            {"doc_id": doc_id},
            {"$set": {"doc_id": doc_id, "domain": domain, "title": source["title"],
                      "filename": filename, "size": len(content),
                      "uploaded_by": admin.id, "uploaded_by_email": admin.email,
                      "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "file": saved_name}},
            upsert=True)
        body = corpus.documents[domain]
        return {"added": doc_id, "domain": domain,
                "format": Path(filename).suffix.lower().lstrip("."), "file": saved_name,
                "sources": len(body["sources"]), "chunks": len(body["chunks"]),
                "scope": scope_msg}

    @app.get("/contracts")
    async def contracts(request: HTTPRequest, admin: Principal = Depends(require_role("admin"))):
        """Integration documentation for service builders — internal, admin-gated."""
        await check_origin(request)
        original = {"query": "How do I reset my password?", "privacy": "local_only",
                    "available": ["it"], "clarify_attempts": 0}
        base = {"request_id": "m1-generated-id", "request": original}
        answer = {"domain": "it", "location": "local",
                  "answer": "Use the IT help portal.", "citations": [{"doc_id": "it-1", "title": "Password help"}],
                  "evidence": [{"doc_id": "it-1", "chunk": "..."}]}
        return {"version": "cortex-rag-v1-proposed", "team_approved": False,
                "transport": "POST configured endpoint; X-Request-ID and Idempotency-Key remain stable across retries.",
                "m1_decision": {"route": {"action": "route", "tasks": [{"domain": "it", "instruction": "..."}], "message": "", "options": []},
                                "clarify": {"action": "clarify", "tasks": [], "message": "Which area?", "options": [{"domain": "it"}, {"domain": "hr"}]},
                                "unsupported_or_handoff": {"action": "unsupported|handoff", "tasks": [], "message": "...", "options": []},
                                "confidence": "computed deterministically by M1 from domain keyword evidence; never model-reported"},
                "domain_skill": {"request": {**base, "domain": "it", "instruction": "..."},
                                 "response": answer,
                                 "no_evidence": "HTTP error or {error:'no_evidence'} when retrieval finds nothing; the skill must abstain, never guess"},
                "m2": {"request": {**base, "domain_answers": [answer], "failures": []},
                       "response": {"response": "merged reply", "citations": [{"doc_id": "it-1", "title": "Password help"}]}},
                "v1": {"request": {**base, "domain_answers": [answer], "response": "merged reply", "citations": []},
                       "response": {"status": "passed|failed|uncertain", "flags": ["example_flag"], "explanation": "Evidence and limitations"}},
                "privacy": "local_only prohibits all non-loopback services, including M2/V1.",
                "stream": {"endpoint": "POST /query/stream", "body": "same as /query",
                           "protocol": "Server-Sent Events; always HTTP 200 — read the final status from the result event",
                           "events": {"stage": '{"event":"stage","stage":"m1|skills|m2|v1","state":"active|done|fail",...}',
                                      "skill": '{"event":"skill","domain":"it","state":"done|abstained|fail","citations":n}',
                                      "error": '{"event":"error","service":"m1","code":"...","hint":"human-readable fix"}',
                                      "result": "final payload, identical to the /query response body",
                                      "done": "stream terminator"}},
                "corpus_upload": "POST /corpus/upload — multipart form {domain, file}; .md/.txt/.pdf/.docx; "
                                 "extracts, chunks and indexes the document in-memory for this process",
                "models": "GET /models — which model powers each pipeline role (or deterministic fallback)",
                "trust": "Queries and skill outputs are untrusted text, never commands or authority to change policy."}

    @app.post("/query")
    async def query(body: QueryBody, request: HTTPRequest,
                    user: Principal = Depends(require_user)):
        await check_origin(request)
        if user.role == "guest":
            # IP-based: a guest could mint a fresh token, the address is the real bound
            request.app.state.guest_query_limiter.check(f"guest:{client_ip(request)}")
        # Guests are pinned to the general knowledge area — their `available`
        # is replaced regardless of what they asked for. `viewer_role` feeds
        # document-level access checks in retrieval.
        available = ("general",) if user.role == "guest" else tuple(body.available)
        try:
            model_request = Request(body.query, body.privacy, available, body.clarify_attempts,
                                    viewer_role=user.role)
            result = await engine.run(model_request)
        except ValueError:
            raise HTTPException(422, "Invalid query or domain selection")
        result["mode"] = mode
        codes = {"busy": 429, "deadline_exceeded": 504, "routing_failed": 503, "failed": 502, "aggregation_unavailable": 503}
        return JSONResponse(result, status_code=codes.get(result["status"], 200))

    @app.post("/query/stream")
    async def query_stream(body: QueryBody, request: HTTPRequest,
                           user: Principal = Depends(require_user)):
        """SSE variant of /query: streams stage/skill telemetry as the pipeline runs,
        then the full result payload. Always 200 — clients read status from the result event."""
        await check_origin(request)
        if user.role == "guest":
            request.app.state.guest_query_limiter.check(f"guest:{client_ip(request)}")
        available = ("general",) if user.role == "guest" else tuple(body.available)
        try:
            model_request = Request(body.query, body.privacy, available, body.clarify_attempts,
                                    viewer_role=user.role)
        except ValueError:
            raise HTTPException(422, "Invalid query or domain selection")
        queue = asyncio.Queue()

        def emit(event):
            queue.put_nowait(event)

        ERROR_HINTS = {"invalid_or_unavailable_router": "routing model unavailable — is Ollama running?",
                       "transport_unavailable": "a backend service was unreachable — check the service is running",
                       "timeout": "a service timed out — the model may still be loading",
                       "no_evidence": "no matching document in the knowledge bases",
                       "rate_limited": "a service was rate limited",
                       "remote_unavailable": "a remote service was unavailable",
                       "missing_service_credentials": "a service is missing its credentials",
                       "deadline_exceeded": "the request took too long overall"}

        async def stream():
            task = asyncio.create_task(engine.run(model_request, on_event=emit))
            try:
                while not (task.done() and queue.empty()):
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=10)
                        yield f"event: {event['event']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"  # comment heartbeat keeps proxies from closing idle streams
                if task.cancelled():
                    return
                if task.exception() is not None:
                    # Never end the stream without a result: the UI only shows
                    # "Something interrupted this response" when no result event
                    # arrives, so emit a degraded result the UI can render.
                    degraded = {"request_id": f"stream-{uuid.uuid4().hex[:12]}",
                                "status": "failed", "response": None, "draft": None,
                                "routing": None, "message": "The pipeline hit an unexpected error before finishing. Please try again.",
                                "skills": [], "citations": [], "errors": [{"service": "pipeline", "code": "transport_unavailable"}],
                                "verification": {"status": "not_run"}, "mock_services": [], "elapsed_ms": 0,
                                "mode": mode}
                    yield f"event: result\ndata: {json.dumps(degraded, ensure_ascii=False)}\n\n"
                    yield 'event: error\ndata: {"service": "pipeline", "code": "transport_unavailable", "hint": "pipeline error"}\n\n'
                    yield "event: done\ndata: {}\n\n"
                    return
                result = task.result()
                result["mode"] = mode
                yield f"event: result\ndata: {json.dumps(result, ensure_ascii=False)}\n\n"
                for error in result.get("errors", []):
                    hint = ERROR_HINTS.get(error["code"], "pipeline error")
                    yield f"event: error\ndata: {json.dumps({'service': error['service'], 'code': error['code'], 'hint': hint})}\n\n"
                yield "event: done\ndata: {}\n\n"
            except asyncio.CancelledError:
                if not task.done():
                    task.cancel()
                raise
            except Exception:
                # Last-resort degraded result — a closed stream with no result
                # is what the UI reports as "interrupted".
                try:
                    degraded = {"request_id": f"stream-{uuid.uuid4().hex[:12]}",
                                "status": "failed", "response": None, "draft": None,
                                "routing": None, "message": "The pipeline hit an unexpected error before finishing. Please try again.",
                                "skills": [], "citations": [], "errors": [{"service": "pipeline", "code": "transport_unavailable"}],
                                "verification": {"status": "not_run"}, "mock_services": [], "elapsed_ms": 0,
                                "mode": mode}
                    yield f"event: result\ndata: {json.dumps(degraded, ensure_ascii=False)}\n\n"
                    yield "event: done\ndata: {}\n\n"
                except Exception:
                    pass

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    _here = Path(__file__).resolve().parent
    # The UI ships as a sibling folder (repo layout: M1/ + Frontend/). Accept either
    # capitalisation and a package-local copy, so the API still serves the client on
    # case-sensitive filesystems (Linux) where "frontend" != "Frontend".
    frontend_dir = next((candidate for candidate in
                         (_here.parent / "Frontend", _here.parent / "frontend",
                          _here / "Frontend", _here / "frontend")
                         if candidate.is_dir()), None)
    if frontend_dir is not None:
        # one front door: the UI is served by the API itself, so browser calls
        # stay same-origin and pass the authorize() origin/loopback checks.
        # Explicit routes win over the static mount: / is the landing page,
        # /app is the assistant (also still reachable as /index.html).
        @app.get("/", include_in_schema=False)
        async def landing():
            return FileResponse(frontend_dir / "landing.html")

        @app.get("/login", include_in_schema=False)
        async def login_page():
            return FileResponse(frontend_dir / "login.html")

        @app.get("/app", include_in_schema=False)
        async def assistant():
            return FileResponse(frontend_dir / "index.html")

        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

    return app


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description="Loopback-only Cortex front-door API")
    parser.add_argument("--mode", choices=("demo", "live"), default="demo")
    parser.add_argument("--retrieval", choices=("semantic", "keyword"), default="semantic",
                        help="demo retrieval backend; semantic falls back to keyword if the embedding model is unavailable")
    parser.add_argument("--llm-merge", action="store_true",
                        help="use the LLM as the M2 merger (needs Ollama; falls back to deterministic merge on failure)")
    parser.add_argument("--llm-verify", action="store_true",
                        help="use the LLM as the V1 verifier (needs Ollama; deterministic citation check always runs)")
    parser.add_argument("--llm-route", action="store_true",
                        help="use the LLM (CORTEX_M1_MODEL, default qwen3:4b) as the M1 router; deterministic guards still apply")
    parser.add_argument("--m1-backcheck", action="store_true",
                        help="implies --llm-route; a second model call restates the question from the decision JSON "
                             "and a low restatement-query cosine demotes the route to clarify "
                             "(threshold via CORTEX_M1_BACKCHECK_MIN, default 0.70 — tuned on labeled data)")
    parser.add_argument("--llm-answer", action="store_true",
                        help="let the LLM write each domain skill's prose from retrieved chunks "
                             "(CORTEX_ANSWER_MODEL, default qwen3:4b; citations/evidence stay code-computed)")
    parser.add_argument("--services", help="Trusted service config, required in live mode")
    parser.add_argument("--m2-url", help="remote M2 merger endpoint, e.g. http://127.0.0.1:9001/merge")
    parser.add_argument("--allow-origin", action="append", default=[], metavar="ORIGIN",
                        help="browser origin allowed to call this API cross-origin (repeatable), e.g. "
                             "--allow-origin http://127.0.0.1:5500 for the VS Code Live Server; also read "
                             "from CORTEX_ALLOWED_ORIGINS (comma-separated). Default: same-origin only.")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    env_origins = [o.strip() for o in os.environ.get("CORTEX_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    allowed_origins = list(dict.fromkeys(env_origins + args.allow_origin))
    app = make_app(mode=args.mode, service_config=args.services, api_token=os.environ.get("CORTEX_API_TOKEN"),
                   retrieval=args.retrieval, llm_merge=args.llm_merge, llm_verify=args.llm_verify,
                   llm_route=args.llm_route or args.m1_backcheck, m1_backcheck=args.m1_backcheck, m2_url=args.m2_url,
                   llm_answer=args.llm_answer, allowed_origins=allowed_origins,
                   metrics_path=Path(__file__).resolve().parent / "data" / "metrics.json",
                   queries_path=Path(__file__).resolve().parent / "data" / "queries.jsonl",
                   tickets_path=Path(__file__).resolve().parent / "data" / "tickets.json",
                   persist_root=Path(__file__).resolve().parent.parent / "dataset" / "corpus.d")
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
