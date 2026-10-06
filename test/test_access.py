"""Access-control tests: corpus governance metadata (sensitivity /
allowed_roles / allowed_users / allowed_entities) mapped onto app tiers and
enforced in retrieval and the corpus API."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from backend.access import TIERS, can_view, required_role
from backend.store import CorpusIndex, load_documents

ROOT = Path(__file__).resolve().parent


class PolicyTests(unittest.TestCase):
    def test_sensitivity_floor(self):
        self.assertEqual(required_role({"sensitivity": "PUBLIC"}), "guest")
        self.assertEqual(required_role({"sensitivity": "INTERNAL"}), "user")
        self.assertEqual(required_role({"sensitivity": "CONFIDENTIAL"}), "admin")
        self.assertEqual(required_role({"sensitivity": "RESTRICTED"}), "admin")

    def test_privileged_roles_raise_to_admin(self):
        meta = {"sensitivity": "INTERNAL", "allowed_roles": ["HR", "ADMIN"]}
        self.assertEqual(required_role(meta), "admin")
        self.assertFalse(can_view(meta, "user"))
        self.assertTrue(can_view(meta, "admin"))

    def test_broad_roles_open_to_members(self):
        meta = {"sensitivity": "INTERNAL",
                "allowed_roles": ["STUDENT", "ADMIN"]}
        self.assertTrue(can_view(meta, "user"))
        self.assertFalse(can_view(meta, "guest"))

    def test_named_entities_are_admin_only(self):
        for meta in ({"sensitivity": "INTERNAL", "allowed_users": ["EMP-1001"]},
                     {"sensitivity": "PUBLIC", "allowed_entities": ["CC-ENG-07"]}):
            self.assertEqual(required_role(meta), "admin")

    def test_missing_metadata_defaults_member_level(self):
        self.assertEqual(required_role({}), "user")
        self.assertTrue(can_view({}, "user"))
        self.assertFalse(can_view({}, "guest"))


class RetrievalEnforcementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = CorpusIndex(load_documents(ROOT.parent / "dataset" / "corpus.json"))

    def test_restricted_doc_invisible_below_admin(self):
        restricted = next(s for b in self.corpus.documents.values() for s in b["sources"].values()
                          if required_role(s.get("metadata", {})) == "admin")
        domain = self._domain_of(restricted["doc_id"])
        query = restricted["title"]
        admin_hits = {h["doc_id"] for h in self.corpus.retrieve(domain, query, viewer="admin")}
        user_hits = {h["doc_id"] for h in self.corpus.retrieve(domain, query, viewer="user")}
        self.assertIn(restricted["doc_id"], admin_hits)
        self.assertNotIn(restricted["doc_id"], user_hits)

    def test_guest_cannot_read_internal_doc(self):
        internal = next(s for b in self.corpus.documents.values() for s in b["sources"].values()
                        if required_role(s.get("metadata", {})) == "user")
        # craft a query rich in the doc's own tokens so it would rank without ACL
        hits = self.corpus.retrieve(self._domain_of(internal["doc_id"]),
                                    internal["title"], viewer="guest")
        self.assertNotIn(internal["doc_id"], {h["doc_id"] for h in hits})

    def _domain_of(self, doc_id):
        for d, b in self.corpus.documents.items():
            if doc_id in b["sources"]:
                return d
        raise AssertionError(doc_id)


class CoverageFloorTests(unittest.TestCase):
    """The keyword abstention floor: off-corpus queries that share campus
    vocabulary must not surface misleading hits."""

    @classmethod
    def setUpClass(cls):
        cls.corpus = CorpusIndex(load_documents(ROOT.parent / "dataset" / "corpus.json"))

    def test_off_vocab_query_abstains(self):
        hits = self.corpus.retrieve("it", "quantum chromodynamics lattice gauge theory")
        self.assertFalse(hits)

    def test_low_coverage_query_abstains(self):
        hits = self.corpus.retrieve(
            "facilities", "what is the unpublished security response route for the data centre")
        self.assertFalse(hits)

    def test_on_corpus_query_still_answers(self):
        hits = self.corpus.retrieve("hr", "annual leave policy carry over days")
        self.assertTrue(hits)


class CorpusApiEnforcementTests(unittest.TestCase):
    """/corpus reflects the caller's tier; restricted docs are invisible below it."""

    def setUp(self):
        from backend.api import make_app
        from fastapi.testclient import TestClient
        corpus = CorpusIndex(load_documents(ROOT.parent / "dataset" / "corpus.json"))
        tmp = Path(tempfile.mkdtemp())
        from backend.services import KeywordRouter, demo_services
        from backend.orchestrator import Orchestrator
        from models.m1 import Request
        engine = Orchestrator(KeywordRouter(corpus), demo_services(corpus),
                              scorer=corpus.scores, domain_titles=corpus.titles())
        from backend.auth import AuthDB
        self.app = make_app(engine=engine, corpus=corpus, auth_db=AuthDB.memory())
        # base_url must be an allowed host — TrustedHostMiddleware rejects "testserver"
        self.client = TestClient(self.app, base_url="http://127.0.0.1")

    def _count(self, headers=None):
        body = self.client.get("/corpus", headers=headers or {}).json()
        return {d: len(b["sources"]) for d, b in body["domains"].items()}

    def test_anonymous_sees_public_only(self):
        counts = self._count()
        self.assertTrue(sum(counts.values()) <= 25, counts)   # 20 PUBLIC + slack
        admin = self._count(self._bearer("admin"))
        self.assertGreater(sum(admin.values()), sum(counts.values()))

    def test_admin_sees_everything(self):
        counts = self._count(self._bearer("admin"))
        self.assertEqual(sum(counts.values()), 528)

    def _bearer(self, role):
        from backend.auth import issue_jwt
        import secrets
        secret = self.app.state.jwt_secret
        if role == "admin":
            email, pw = "acl-admin@test.edu", "Passw0rd!234"
            self.app.state.auth_db.create(email, name="A", password=pw, role="admin")
            token = self.client.post("/auth/login",
                                     json={"email": email, "password": pw}).json()["token"]
        else:
            token = self.client.post("/auth/guest").json()["token"]
        return {"Authorization": f"Bearer {token}"}


if __name__ == "__main__":
    unittest.main()
