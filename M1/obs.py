"""Centralized structured observability for Cortex — one logger, no per-module copies.

Every event carries the request-scoped request_id (plus user id/role once auth
resolves them) via contextvars, so concurrent async requests never cross streams.
Stages emit START/END/ERROR with monotonic timings; each request ends with a
PIPELINE_SUMMARY so a single `grep request_id=` reconstructs the whole run.

Config (env):
  LOG_LEVEL=INFO          DEBUG for the human-readable pipeline trace
  LOG_FORMAT=kv|json      kv is default (readable), json for log pipelines
  LOG_TO_FILE=false       true → rotating logs/app.log (5MB x3)
  LOG_QUERY_CONTENT=false true → log the query text, not just its length
  LOG_MODEL_OUTPUT=false  true → log model output text (truncated)
  LOG_RETRIEVED_DOCS=false true → log doc/chunk ids per retrieval
"""

import json
import logging
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler

LOG = logging.getLogger("cortex")
LOG.propagate = False

# request-scoped state — contextvars keep concurrent asyncio/gather requests apart
_RID: ContextVar = ContextVar("request_id", default=None)
_UID: ContextVar = ContextVar("user_id", default=None)
_ROLE: ContextVar = ContextVar("user_role", default=None)
_STAGES: ContextVar = ContextVar("stages", default=None)

SENSITIVE_KEY = re.compile(r"pass|token|secret|key|authoriz|credential|cookie|signature", re.I)
MAX_FIELD = 300


def _truthy(name, default="false"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def cfg():
    return {"query_content": _truthy("LOG_QUERY_CONTENT"),
            "model_output": _truthy("LOG_MODEL_OUTPUT"),
            "retrieved_docs": _truthy("LOG_RETRIEVED_DOCS")}


# ── request context ───────────────────────────────────────────────────────────

def begin_request(request_id=None):
    """Start a request scope. Returns a token for end_request()."""
    rid = request_id or uuid.uuid4().hex[:12]
    _STAGES.set([])
    _UID.set(None)
    _ROLE.set(None)
    return _RID.set(rid)


def end_request(token):
    if token is not None:
        _RID.reset(token)
    _STAGES.set(None)
    _UID.set(None)
    _ROLE.set(None)


def ensure_request():
    """Non-HTTP callers (tests, scripts) still get a rid + stage list."""
    if _RID.get() is None:
        _RID.set(uuid.uuid4().hex[:12])
    if _STAGES.get() is None:
        _STAGES.set([])
    return _RID.get()


def request_id():
    return _RID.get()


def set_user(user_id, role):
    _UID.set(user_id)
    _ROLE.set(role)


# ── field hygiene ─────────────────────────────────────────────────────────────

def _clean(name, value):
    """Never let a secret-shaped field reach the log; truncate everything else."""
    if SENSITIVE_KEY.search(str(name)):
        return "[redacted]"
    if isinstance(value, str):
        return value[:MAX_FIELD] + "…" if len(value) > MAX_FIELD else value
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, default=str)
        return text[:MAX_FIELD] + "…" if len(text) > MAX_FIELD else text
    return value


def _kv(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text.startswith(("[", "{")):
        return text   # already JSON-serialized by _clean — print it as-is
    return text if re.fullmatch(r"[\w.\-:/]+", text) else json.dumps(text, ensure_ascii=False)


class _KvFormatter(logging.Formatter):
    def format(self, record):
        line = f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')}.{int(record.msecs):03d} " \
               f"{record.levelname:<7} {record.getMessage()}"
        data = getattr(record, "obs", None)
        if data:
            line += " " + " ".join(f"{k}={_kv(v)}" for k, v in data.items())
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        body = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}",
                "level": record.levelname, **(getattr(record, "obs", {}) or {})}
        body["event"] = record.getMessage()
        if record.exc_info:
            body["exception"] = self.formatException(record.exc_info)
        return json.dumps(body, ensure_ascii=False)


_configured = False


def configure(force=False):
    """Idempotent: attach stderr (+ optional rotating file) once."""
    global _configured
    if _configured and not force:
        return LOG
    _configured = True
    LOG.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))
    LOG.handlers.clear()
    fmt = _JsonFormatter() if os.environ.get("LOG_FORMAT", "kv").lower() == "json" else _KvFormatter()
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    LOG.addHandler(console)
    if _truthy("LOG_TO_FILE"):
        try:
            from pathlib import Path
            log_dir = Path(__file__).parent.parent / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(log_dir / "app.log", maxBytes=5_000_000,
                                               backupCount=3, encoding="utf-8")
            file_handler.setFormatter(fmt)
            LOG.addHandler(file_handler)
        except OSError:
            LOG.warning("LOG_TO_FILE requested but logs/ is not writable — console only")
    return LOG


# ── event helpers ─────────────────────────────────────────────────────────────

def event(name, level=logging.INFO, exc=None, **fields):
    """Emit one structured event. NEVER allowed to raise into business logic."""
    try:
        data = {"request_id": _RID.get() or "-"}
        if _UID.get():
            data["user_id"], data["role"] = _UID.get(), _ROLE.get()
        data.update((k, _clean(k, v)) for k, v in fields.items())
        LOG.log(level, name, extra={"obs": data},
                exc_info=(exc if exc is not None else None))
    except Exception:
        pass


def _record_stage(name, ms, state):
    stages = _STAGES.get()
    if stages is not None:
        stages.append({"name": name, "ms": ms, "state": state})


@contextmanager
def stage(name, **meta):
    """with obs.stage("M1", model=...) as out: ... out.update(result_fields)

    Emits NAME_START, NAME_END (latency_ms + whatever the caller put in `out`,
    plus out["state"]="fail" marking), or NAME_ERROR with traceback on raise."""
    ensure_request()
    event(f"{name}_START", **meta)
    t0 = time.perf_counter()
    out = {}
    try:
        yield out
    except Exception as err:
        ms = round((time.perf_counter() - t0) * 1000, 1)
        _record_stage(name, ms, "error")
        event(f"{name}_ERROR", level=logging.ERROR, exc=err, latency_ms=ms,
              error_type=type(err).__name__, error=str(err)[:MAX_FIELD])
        raise
    ms = round((time.perf_counter() - t0) * 1000, 1)
    state = out.pop("state", "ok")
    _record_stage(name, ms, state)
    event(f"{name}_END", latency_ms=ms, state=state, **out)


def stage_error(name, err, **meta):
    """Standalone stage-failure event for code paths not wrapped in `stage()`."""
    _record_stage(name, 0, "error")
    event(f"{name}_ERROR", level=logging.ERROR, exc=err,
          error_type=type(err).__name__, error=str(err)[:MAX_FIELD], **meta)


def auth_rejected(reason, status, **meta):
    event("AUTH_REJECTED", level=logging.WARNING, reason=reason, http_status=status, **meta)


def log_query(query, **meta):
    fields = {"query_length": len(query)}
    if cfg()["query_content"]:
        fields["query"] = query
    else:
        fields["query_sha256_12"] = __import__("hashlib").sha256(query.encode()).hexdigest()[:12]
    event("QUERY_RECEIVED", **fields, **meta)


def log_flags(flags, **meta):
    """FLAGS_GENERATED — every flag name verbatim plus what it fed into."""
    event("FLAGS_GENERATED", flag_count=len(flags), flags=list(flags), **meta)


def log_decision(action, reason, **meta):
    event("ROUTING_DECISION", action=action, reason=reason, **meta)


def log_model_output(tag, text):
    if cfg()["model_output"]:
        event(tag, output=text)


# ── summaries ─────────────────────────────────────────────────────────────────

def pipeline_summary(status, total_ms, **extra):
    stages = _STAGES.get() or []
    fields = {"status": status, "total_ms": total_ms,
              "stage_order": ">".join(s["name"].lower() for s in stages)}
    for s in stages:
        fields[f"{s['name'].lower()}_ms"] = s["ms"]
    event("PIPELINE_SUMMARY", **fields, **extra)


def pipeline_trace():
    """Human-readable ✓/✗/⚠ trace — DEBUG only, for dev terminals."""
    stages = _STAGES.get() or []
    mark = {"ok": "✓", "error": "✗"}.get
    lines = "\n".join(f"  {mark(s['state'], '⚠')} {s['name']:<12} {s['ms']:>8.1f}ms" for s in stages)
    event("PIPELINE_TRACE", level=logging.DEBUG, trace="\n" + lines)


configure()
