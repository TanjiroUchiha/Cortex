"""Observability tests: structured events, request_id scoping, stage timings,
redaction, env toggles, middleware correlation, and failure-isolation."""
import asyncio
import io
import logging
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("CORTEX_JWT_SECRET", "x" * 40)

import api
import obs
from fastapi.testclient import TestClient
from test_auth import build


class Capture:
    """Attach a memory handler to the shared 'cortex' logger for one test."""

    def __init__(self):
        self.buf = io.StringIO()
        self.handler = logging.StreamHandler(self.buf)
        self.handler.setFormatter(obs._KvFormatter())

    def __enter__(self):
        obs.LOG.addHandler(self.handler)
        return self.buf

    def __exit__(self, *_):
        obs.LOG.removeHandler(self.handler)


def rid_of(line):
    for part in line.split():
        if part.startswith("request_id="):
            return part.split("=", 1)[1]
    return None


class T(unittest.TestCase):

    # ── events + fields ──────────────────────────────────────────────────────

    def test_event_carries_request_id_and_fields(self):
        with Capture() as buf:
            token = obs.begin_request()
            obs.event("M1_END", model="qwen3:4b", latency_ms=42.5, state="ok")
            obs.end_request(token)
        line = buf.getvalue().strip()
        self.assertIn("M1_END", line)
        self.assertIn("model=qwen3:4b", line)
        self.assertIn("latency_ms=42.5", line)
        self.assertIsNotNone(rid_of(line))

    def test_stage_emits_start_end_with_latency_and_caller_fields(self):
        with Capture() as buf:
            obs.ensure_request()
            with obs.stage("RETRIEVAL", domain="it", engine="keyword") as out:
                out.update(hits=3, score_max=0.9)
            obs.end_request(None)
        text = buf.getvalue()
        self.assertIn("RETRIEVAL_START", text)
        self.assertIn("RETRIEVAL_END", text)
        self.assertIn("latency_ms=", text)
        self.assertIn("hits=3", text)

    def test_stage_error_logs_and_reraises(self):
        with Capture() as buf:
            obs.ensure_request()
            with self.assertRaises(TimeoutError):
                with obs.stage("M2", service="local"):
                    raise TimeoutError("merge timed out")
            obs.end_request(None)
        self.assertIn("M2_ERROR", buf.getvalue())
        self.assertIn("TimeoutError", buf.getvalue())

    def test_sensitive_fields_redacted(self):
        with Capture() as buf:
            obs.ensure_request()
            obs.event("X", password="hunter2", jwt_token="abc.def",
                      authorization="Bearer x", path="/query")
            obs.end_request(None)
        text = buf.getvalue()
        self.assertNotIn("hunter2", text)
        self.assertNotIn("abc.def", text)
        self.assertIn("[redacted]", text)
        self.assertIn("path=/query", text)

    def test_query_content_gated_by_env(self):
        with Capture() as buf:
            obs.ensure_request()
            os.environ["LOG_QUERY_CONTENT"] = "false"
            obs.log_query("where are my payslips?")
            os.environ["LOG_QUERY_CONTENT"] = "true"
            obs.log_query("where are my payslips?")
            del os.environ["LOG_QUERY_CONTENT"]
            obs.end_request(None)
        text = buf.getvalue()
        self.assertEqual(text.count("where are my payslips"), 1)   # only the second call
        self.assertIn("query_sha256_12=", text)

    def test_flags_and_decision_events(self):
        with Capture() as buf:
            obs.ensure_request()
            obs.log_flags(["ungrounded_citation", "missing_topic"], verdict="failed")
            obs.log_decision("REJECT", reason="v1:failed")
            obs.end_request(None)
        text = buf.getvalue()
        self.assertIn("FLAGS_GENERATED", text)
        self.assertIn("ungrounded_citation", text)
        self.assertIn("ROUTING_DECISION", text)
        self.assertIn("reason=v1:failed", text)

    def test_pipeline_summary_lists_stage_timings(self):
        with Capture() as buf:
            obs.ensure_request()
            with obs.stage("M1"):
                pass
            with obs.stage("RETRIEVAL"):
                pass
            obs.pipeline_summary("completed", 123.4, skills=2, citations=5)
            obs.end_request(None)
        text = buf.getvalue()
        self.assertIn("PIPELINE_SUMMARY", text)
        self.assertIn("m1_ms=", text)
        self.assertIn("retrieval_ms=", text)
        self.assertIn("total_ms=123.4", text)

    # ── request_id scoping ───────────────────────────────────────────────────

    def test_concurrent_requests_have_isolated_ids(self):
        seen = {}

        async def worker(name):
            token = obs.begin_request()
            seen[name] = []
            await asyncio.sleep(0)          # yield so the tasks interleave
            obs.event("STEP", tag=name)
            obs.end_request(token)

        async def both():
            await asyncio.gather(worker("a"), worker("b"))

        with Capture() as buf:
            asyncio.run(both())
        text = buf.getvalue()
        rids = {rid_of(l) for l in text.strip().splitlines()}
        self.assertEqual(len(rids), 2)      # each request got its own id, no bleed

    # ── middleware ───────────────────────────────────────────────────────────

    def test_middleware_scopes_and_ends_request(self):
        app = build(Path(tempfile.mkdtemp()))
        client = TestClient(app, base_url="http://127.0.0.1")
        with Capture() as buf:
            client.get("/health")
        text = buf.getvalue()
        self.assertIn("REQUEST_START", text)
        self.assertIn("RESPONSE_READY", text)
        self.assertIn("REQUEST_END", text)
        lines = [l for l in text.strip().splitlines() if "request_id=" in l]
        self.assertTrue(lines)
        self.assertEqual({rid_of(l) for l in lines}, {rid_of(lines[0])})

    def test_auth_rejection_logged_with_reason(self):
        app = build(Path(tempfile.mkdtemp()))
        client = TestClient(app, base_url="http://127.0.0.1")
        with Capture() as buf:
            client.get("/domains")                      # no token → 401
            client.get("/metrics")                      # same
        text = buf.getvalue()
        self.assertIn("AUTH_REJECTED", text)
        self.assertIn("reason=missing_or_invalid_token", text)
        self.assertIn("http_status=401", text)

    # ── safety ───────────────────────────────────────────────────────────────

    def test_logging_failure_never_raises(self):
        class Boom(logging.Handler):
            def emit(self, record):
                raise RuntimeError("log backend dead")

        boom = Boom()
        obs.LOG.addHandler(boom)
        try:
            obs.ensure_request()
            obs.event("ANYTHING", data="x")          # must not propagate
            with obs.stage("M1"):
                pass                                  # must not propagate
        finally:
            obs.LOG.removeHandler(boom)
            obs.end_request(None)


if __name__ == "__main__":
    unittest.main()
