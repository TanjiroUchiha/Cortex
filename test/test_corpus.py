"""Expanded-corpus integration tests: package loading, frontmatter metadata,
moved-category provenance, version selection, the consolidated routing
surfaces (11 package folders fold into 6 domains), and abstention."""
import asyncio
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT.parent / "dataset" / "corpus.d"
EVAL = ROOT.parent / "dataset" / "evaluation"

from backend.corpus_package import package_sources, parse_markdown
from models import m1
from models.m1 import DOMAINS, DOMAIN_METADATA, PACKAGE_DOMAIN, Request
from backend.services import KeywordRouter, demo_services
from backend.store import CorpusIndex, document_source, load_documents
from backend.orchestrator import Orchestrator

EXPECTED_DOMAINS = {"it", "hr", "fees", "facilities", "general", "academics"}
FOLDER_COUNTS = {"academics": 69, "facilities": 106, "fees": 36, "finance": 20,
                 "general": 50, "hr": 59, "it": 50, "library": 21,
                 "research": 48, "security": 18, "transport": 25}
MOVED = {"hr": "admissions", "general": "student-services",
         "facilities": "hostel", "academics": "labs"}
# Effective-domain package counts after the folder->domain merge.
MERGED_COUNTS = {"it": 50, "hr": 59, "fees": 36 + 20,
                 "facilities": 106 + 18 + 25, "general": 50 + 21,
                 "academics": 69 + 48}
EVAL_COUNTS = {"retrieval-questions.json": 45, "governance-questions.json": 35,
               "multi-document-questions.json": 20, "negative-questions.json": 30}


def load_corpus():
    return CorpusIndex(load_documents(ROOT.parent / "dataset" / "corpus.json"))


@unittest.skipUnless((PACKAGE / "manifest.json").is_file(), "expanded corpus not imported")
class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))
        cls.corpus = load_corpus()

    def test_manifest_count(self):
        self.assertEqual(len(self.manifest["documents"]), 502)
        self.assertTrue({d["category"] for d in self.manifest["documents"]} <= set(PACKAGE_DOMAIN))

    def test_package_doc_counts(self):
        counts = {}
        for category, _ in package_sources(PACKAGE):
            counts[category] = counts.get(category, 0) + 1
        self.assertEqual(counts, FOLDER_COUNTS)

    def test_folders_merge_into_routing_domains(self):
        merged = {}
        for category, _ in package_sources(PACKAGE):
            domain = PACKAGE_DOMAIN[category]
            merged[domain] = merged.get(domain, 0) + 1
        self.assertEqual(merged, MERGED_COUNTS)
        self.assertEqual(set(merged), EXPECTED_DOMAINS)

    def test_package_category_metadata_preserved(self):
        # The folder name stays on the doc as metadata.category — finance/transport
        # docs keep their package category even though they route elsewhere.
        for category, source in package_sources(PACKAGE):
            self.assertEqual(source["metadata"]["category"], category)

    def test_package_sources_merged(self):
        package_ids = {s["id"] for _, s in package_sources(PACKAGE)}
        indexed = {doc_id for body in self.corpus.documents.values()
                   for doc_id in body["sources"]}
        self.assertTrue(package_ids <= indexed)
        # seed (14) + the corpus.d drop-ins the package did not supersede (12);
        # colliding drop-ins were upgraded in place to their package versions
        self.assertEqual(len(indexed), len(package_ids) + 26)

    def test_frontmatter_not_indexed_as_prose(self):
        for body in self.corpus.documents.values():
            for s in body["sources"].values():
                self.assertFalse(s["content"].lstrip().startswith("---"),
                                 f"frontmatter leaked into content: {s['doc_id']}")
                self.assertNotIn("allowed_roles:", s["content"])

    def test_metadata_structured(self):
        src = self.corpus.documents["academics"]["sources"]["ACAD-ACADEMIC-ADVISING"]
        m = src["metadata"]
        self.assertEqual(m["document_id"], "ACAD-ACADEMIC-ADVISING")
        self.assertEqual(m["department"], "Academic Affairs")
        self.assertEqual(m["category"], "academics")
        self.assertIsInstance(m["allowed_roles"], list)
        self.assertEqual(src["relative_path"], "academics/academic-advising.md")

    def test_moved_documents_keep_provenance(self):
        moved = [s for body in self.corpus.documents.values() for s in body["sources"].values()
                 if s.get("metadata", {}).get("original_category")]
        self.assertTrue(len(moved) >= 100)
        for s in moved:
            m = s["metadata"]
            self.assertNotEqual(m["original_category"], m["category"])
            self.assertTrue(m["department"])
        for domain, prior in MOVED.items():
            self.assertTrue(any(s["metadata"].get("original_category") == prior
                                for s in self.corpus.documents[domain]["sources"].values()),
                            f"no {prior} doc preserved under {domain}")

    def test_version_links_and_active_precedence(self):
        superseded = {doc_id: s for body in self.corpus.documents.values()
                      for doc_id, s in body["sources"].items()
                      if s.get("metadata", {}).get("status") == "superseded"}
        self.assertTrue(superseded)
        for doc_id, s in superseded.items():
            m = s["metadata"]
            self.assertTrue(m.get("superseded_by"), f"{doc_id} missing superseded_by link")
            domain = self._domain_of(doc_id)
            generic = re.sub(r"\b(?:19|20)\d{2}\b", "", s["title"])  # drop the doc's own year
            hits = self.corpus.retrieve(domain, generic)
            self.assertNotIn(doc_id, {h["doc_id"] for h in hits},
                             f"superseded doc {doc_id} surfaced for a generic query")
            chunk = next(c for c in self.corpus.documents[domain]["chunks"]
                         if c["doc_id"] == doc_id)
            self.assertFalse(self.corpus.version_eligible(domain, chunk, generic))
            year = re.search(r"\b(?:19|20)\d{2}\b", s["title"])
            if year:  # a query explicitly asking for that year may still reach it
                self.assertTrue(self.corpus.version_eligible(
                    domain, chunk, f"{generic} {year.group(0)}"))

    def _domain_of(self, doc_id):
        for d, body in self.corpus.documents.items():
            if doc_id in body["sources"]:
                return d
        raise AssertionError(doc_id)

    def test_related_document_links_preserved(self):
        linked = [s for body in self.corpus.documents.values() for s in body["sources"].values()
                  if s.get("metadata", {}).get("related_documents")]
        self.assertTrue(linked)
        for s in linked:
            self.assertIsInstance(s["metadata"]["related_documents"], list)

    def test_eval_questions(self):
        total = 0
        for name, expected in EVAL_COUNTS.items():
            path = EVAL / name
            self.assertTrue(path.is_file(), name)
            questions = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(questions, dict):
                questions = questions.get("questions", questions.get("items", []))
            self.assertEqual(len(questions), expected, name)
            total += len(questions)
        self.assertEqual(total, 130)


class DomainSurfaceTests(unittest.TestCase):
    def test_domain_registry(self):
        self.assertEqual(set(DOMAINS), EXPECTED_DOMAINS)
        for d in DOMAINS:
            self.assertTrue(DOMAIN_METADATA[d]["title"])
            self.assertTrue(DOMAIN_METADATA[d]["keywords"])

    def test_schema_limits_cover_all_domains(self):
        variants = m1.DECISION_SCHEMA["oneOf"]
        for v in variants:
            for key in ("tasks", "options"):
                limit = v["properties"][key].get("maxItems", 0)
                if v["properties"][key].get("minItems"):
                    self.assertGreaterEqual(limit, len(DOMAINS), key)

    def test_six_domain_decision_parses(self):
        decision = {"action": "route", "message": "", "options": [],
                    "tasks": [{"domain": d, "instruction": f"part for {d}"}
                              for d in DOMAINS]}
        parsed = m1.parse_decision(json.dumps(decision),
                                   Request(query="multi-domain test"))
        self.assertEqual(len(parsed["tasks"]), 6)

    def test_request_accepts_all_domains(self):
        self.assertEqual(Request(query="q", available=list(DOMAINS)).available, DOMAINS)

    def test_m2_accepts_all_domains(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("m2_schemas", ROOT.parent / "models" / "m2" / "schemas.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.DOMAINS, EXPECTED_DOMAINS)


class ParserTests(unittest.TestCase):
    def test_parse_frontmatter(self):
        text = "---\ndocument_id: X-1\ncategory: it\nstatus: active\nallowed_roles:\n  - STUDENT\n---\n# Title\nBody."
        meta, prose = parse_markdown(text)
        self.assertEqual(meta["document_id"], "X-1")
        self.assertEqual(meta["allowed_roles"], ["STUDENT"])
        self.assertEqual(prose, "# Title\nBody.")

    def test_no_frontmatter(self):
        meta, prose = parse_markdown("# Heading\nplain text")
        self.assertEqual(meta, {})
        self.assertTrue(prose.startswith("# Heading"))

    def test_bad_frontmatter_rejected(self):
        for text in ("---\nunterminated", "---\n[not a map]\n---\nbody",
                     "---\nkey: 1\nkey: 2\n---\nbody"):
            with self.assertRaises(ValueError, msg=text):
                parse_markdown(text)

    def test_document_source_category_guard(self):
        with self.assertRaises(ValueError):
            document_source("x.md", "---\ncategory: hr\n---\n# T\nbody", "it")
        # a consolidated package category is accepted under its routed domain
        s = document_source("x.md", "---\ncategory: finance\n---\n# T\nbody", "fees")
        self.assertEqual(s["metadata"]["category"], "finance")


@unittest.skipUnless((PACKAGE / "manifest.json").is_file(), "expanded corpus not imported")
class AbstentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = load_corpus()

    def test_off_corpus_query_abstains(self):
        result = asyncio.run(self._ask("What is the airspeed velocity of an unladen swallow?"))
        self.assertIn(result["status"],
                      ("unsupported", "clarify", "no_evidence", "handoff"),
                      result["status"])

    def test_unrelated_domain_has_no_evidence(self):
        hits = self.corpus.retrieve("general", "quantum chromodynamics lattice calculations")
        self.assertFalse(hits)

    async def _ask(self, query):
        engine = Orchestrator(KeywordRouter(self.corpus), demo_services(self.corpus),
                              scorer=self.corpus.scores, domain_titles=self.corpus.titles())
        return await engine.run(Request(query=query))


class EvaluationCliTests(unittest.TestCase):
    def test_role_flag_forms(self):
        from test.evaluate_corpus import parse_args
        for args in (["keyword", "--role", "user"], ["keyword", "--role=user"]):
            parsed = parse_args(args)
            self.assertEqual(parsed.role, "user")
            self.assertEqual(parsed.retrieval, "keyword")

    def test_default_pipeline_runs_output_checks(self):
        from backend.services import CheckVerifier
        self.assertIsInstance(demo_services(load_corpus())["v1"].handler, CheckVerifier)


class QueryApiRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = load_corpus()

    def test_query_and_stream_agree_without_database_access(self):
        from fastapi.testclient import TestClient
        from backend.api import make_app
        from backend.auth import AuthDB, Principal, require_user
        engine = Orchestrator(KeywordRouter(self.corpus), demo_services(self.corpus),
                              scorer=self.corpus.scores, domain_titles=self.corpus.titles())
        app = make_app(engine=engine, corpus=self.corpus, auth_db=AuthDB.memory())
        app.dependency_overrides[require_user] = lambda: Principal("test", "", "Test", "user", "service")
        with TestClient(app, base_url="http://127.0.0.1") as client:
            self.assertEqual(client.get("/health").status_code, 200)
            body = {"query": "when will I get my salary"}
            direct = client.post("/query", json=body)
            stream = client.post("/query/stream", json=body)
            self.assertEqual(direct.status_code, 200)
            self.assertEqual(stream.status_code, 200)
            frames = stream.text.split("\n\n")
            results = [json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))
                       for frame in frames if "event: result\n" in frame]
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["response"], direct.json()["response"])
            self.assertEqual(results[0]["status"], "completed")


class AnswerQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = load_corpus()

    def test_summary_does_not_pad_or_promote_unrelated_times(self):
        answer = "Salary is credited on the last working day of each month."
        text = answer + " Library opens at 8am. This record applies to eligible employees."
        self.assertEqual(self.corpus.summarize("when will I get my salary", [{"content": text}]), answer)

    def test_fee_deadline_does_not_prefer_contact_numbers(self):
        text = "Fee questions go to the office at ext 4500. The fee deadline is October 15."
        summary = self.corpus.summarize("when is the fee deadline", [{"content": text}])
        self.assertEqual(summary, "The fee deadline is October 15.")

    def test_empty_or_heading_only_evidence_abstains(self):
        for evidence in ([], [{"content": "## Summary"}], [{"content": "Library opens at 8am."}]):
            with self.subTest(evidence=evidence):
                self.assertEqual(self.corpus.summarize("salary", evidence), "")

    def test_cleaner_keeps_short_steps_and_drops_labels(self):
        text = "**Purpose and scope**\nReset your password.\n\n1. Open the portal\n2. Select password reset"
        cleaned = self.corpus._clean_sentences(text)
        self.assertNotIn("Purpose", " ".join(cleaned))
        self.assertIn("Open the portal", cleaned)
        self.assertIn("Select password reset", cleaned)

    def test_deduplication_preserves_negation(self):
        text = "Salary is available online. Salary is not available online."
        summary = self.corpus.summarize("salary", [{"content": text}])
        self.assertIn("Salary is available online.", summary)
        self.assertIn("Salary is not available online.", summary)

    def test_handler_summarizes_only_the_query(self):
        from unittest.mock import patch
        with patch.object(self.corpus, "summarize", wraps=self.corpus.summarize) as summarize:
            asyncio.run(self.corpus.handler("hr")({
                "request": {"query": "when will I get my salary", "viewer_role": "user"},
                "instruction": "Answer the HR and Admissions part using only retrieved sources."
            }, "test-summary"))
        self.assertEqual(summarize.call_args.args[0], "when will I get my salary")

    def test_heading_only_handler_returns_no_evidence(self):
        from unittest.mock import patch
        from backend.orchestrator import ServiceError
        hit = {"content": "## Summary", "chunk": "## Summary", "doc_id": "x", "title": "X"}
        with patch.object(self.corpus, "retrieve", return_value=[hit]):
            with self.assertRaisesRegex(ServiceError, "no_evidence"):
                asyncio.run(self.corpus.handler("hr")({"request": {"query": "salary"}}, "test-empty"))

    def test_keyword_retrieval_uses_explicit_query(self):
        query = "when will I get my salary"
        expected = self.corpus.retrieve("hr", query, viewer="user")
        actual = self.corpus.retrieve("hr", "admissions application eligibility", viewer="user", query=query)
        self.assertEqual(actual, expected)

    def test_typo_routes_and_retrieves(self):
        for query, domain in (("how to apply for scolarship", "fees"),
                              ("how do I reset my pasword", "it")):
            with self.subTest(query=query):
                route = self.corpus.classify(query)
                # the expected domain must lead; a strong-evidence rival may
                # join as a second task (dual-coverage topics)
                self.assertEqual(route["tasks"][0]["domain"], domain)
                self.assertTrue(self.corpus.retrieve(domain, query, viewer="user"))

    def test_real_pipeline_salary_and_library(self):
        from backend.services import CheckVerifier
        engine = Orchestrator(KeywordRouter(self.corpus),
                              demo_services(self.corpus, verifier=CheckVerifier()),
                              scorer=self.corpus.scores, domain_titles=self.corpus.titles())
        for query, expected in (("when will I get my salary", "last working day"),
                                ("when does the library close", "10pm"),
                                ("what are the library opening hours", "10pm")):
            with self.subTest(query=query):
                result = asyncio.run(engine.run(Request(query=query, viewer_role="user")))
                self.assertEqual(result["status"], "completed", result.get("verification"))
                self.assertIn(expected, result["response"])
                self.assertNotIn("This record applies", result["response"])


if __name__ == "__main__":
    unittest.main()
