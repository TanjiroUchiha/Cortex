"""Auth tests: JWT issue/verify, role gating, Google sign-in, rate limits.

Runs against make_app with a stub engine — auth rejects happen before the
pipeline is touched, so the engine never needs to answer a query.
"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("CORTEX_JWT_SECRET", "test-secret-" + "x" * 40)

import jwt as pyjwt
from fastapi.testclient import TestClient

from backend import api
from backend import auth


class _Engine:
    """Minimum shape make_app's routes touch."""
    services = {}
    domain_titles = {}
    router = None
    metrics = {}
    feedback = {}
    metrics_path = queries_path = tickets_path = None

    last_request = None

    async def run(self, request, on_event=None):
        _Engine.last_request = request
        return {"status": "completed", "errors": []}


def build(tmp: Path, **kw):
    """Fresh app + fresh DB per test so the in-memory rate limiter resets."""
    return api.make_app(engine=_Engine(), mode="demo",
                        auth_db=auth.AuthDB.memory(), **kw)


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = build(self.tmp)
        # base_url must be an allowed host — TrustedHostMiddleware rejects "testserver"
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.db = self.app.state.auth_db

    def tearDown(self):
        self.db.close()

    def make_user(self, email="ada@campus.edu", role="user", password="correct-horse-1"):
        return self.db.create(email, name="Ada", password=password, role=role)

    def token_for(self, user):
        return auth.issue_jwt(user, self.app.state.jwt_secret)

    def bearer(self, token):
        return {"Authorization": f"Bearer {token}"}

    def login(self, email, password):
        return self.client.post("/auth/login", json={"email": email, "password": password})

    # ── local login ──────────────────────────────────────────────────────────

    def test_login_returns_jwt(self):
        u = self.make_user()
        r = self.login("ada@campus.edu", "correct-horse-1")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["user"]["role"], "user")
        claims = pyjwt.decode(body["token"], self.app.state.jwt_secret,
                              algorithms=["HS256"], issuer=auth.ISSUER)
        self.assertEqual(claims["sub"], u.id)

    def test_wrong_password_generic_401(self):
        self.make_user()
        for email in ("ada@campus.edu", "nobody@campus.edu"):   # known + unknown email
            r = self.login(email, "wrong-password")
            self.assertEqual(r.status_code, 401)
            self.assertEqual(r.json()["detail"], "Incorrect email or password")

    def test_protected_requires_token(self):
        self.assertEqual(self.client.get("/domains").status_code, 401)
        self.assertEqual(self.client.post("/query", json={"query": "hi"}).status_code, 401)
        self.assertEqual(self.client.get("/metrics").status_code, 401)

    def test_public_endpoints_open(self):
        self.assertEqual(self.client.get("/health").status_code, 200)

    # ── token tampering ──────────────────────────────────────────────────────

    def domains(self, token):
        return self.client.get("/domains", headers=self.bearer(token))

    def test_expired_token_401(self):
        u = self.make_user()
        import time
        tok = pyjwt.encode({"sub": u.id, "role": "user", "iat": int(time.time()) - 4000,
                            "exp": int(time.time()) - 3400, "iss": auth.ISSUER},
                           self.app.state.jwt_secret, algorithm="HS256")
        self.assertEqual(self.domains(tok).status_code, 401)

    def test_malformed_forged_wrong_iss_alg_none(self):
        u = self.make_user()
        self.assertEqual(self.domains("not-a-jwt").status_code, 401)
        forged = pyjwt.encode({"sub": u.id, "role": "user", "iat": 1, "exp": 9999999999,
                               "iss": auth.ISSUER}, "attacker-key-" + "y" * 40, algorithm="HS256")
        self.assertEqual(self.domains(forged).status_code, 401)
        wrong_iss = pyjwt.encode({"sub": u.id, "role": "user", "iat": 1, "exp": 9999999999,
                                  "iss": "evil"}, self.app.state.jwt_secret, algorithm="HS256")
        self.assertEqual(self.domains(wrong_iss).status_code, 401)
        none_alg = pyjwt.encode({"sub": u.id, "role": "admin", "iss": auth.ISSUER},
                                None, algorithm="none")
        self.assertEqual(self.domains(none_alg).status_code, 401)

    def test_role_claim_is_cosmetic(self):
        """A token claiming admin on a user-role account still gets 403 — DB wins."""
        u = self.make_user(role="user")
        import time
        tok = pyjwt.encode({"sub": u.id, "role": "admin", "iat": int(time.time()),
                            "exp": int(time.time()) + 600, "iss": auth.ISSUER},
                           self.app.state.jwt_secret, algorithm="HS256")
        self.assertEqual(self.client.get("/admin/users", headers=self.bearer(tok)).status_code, 403)

    def test_deactivated_user_cut_off(self):
        u = self.make_user()
        token = self.token_for(u)
        self.assertEqual(self.domains(token).status_code, 200)
        self.db.set_active(u.id, False)
        self.assertEqual(self.domains(token).status_code, 401)

    # ── roles ────────────────────────────────────────────────────────────────

    def test_user_cannot_admin_endpoint(self):
        u = self.make_user()
        self.assertEqual(self.client.get("/metrics", headers=self.bearer(self.token_for(u))).status_code, 403)

    def test_admin_reaches_admin_endpoint(self):
        u = self.make_user(email="boss@campus.edu", role="admin")
        r = self.client.get("/admin/users", headers=self.bearer(self.token_for(u)))
        self.assertEqual(r.status_code, 200)
        self.assertIn("users", r.json())

    def test_admin_creates_user_and_user_signs_in(self):
        admin = self.make_user(email="boss@campus.edu", role="admin")
        r = self.client.post("/admin/users", headers=self.bearer(self.token_for(admin)),
                             json={"email": "new@campus.edu", "password": "new-password-1",
                                   "name": "New", "role": "user"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.login("new@campus.edu", "new-password-1").status_code, 200)

    def test_admin_cannot_demote_or_deactivate_self(self):
        admin = self.make_user(email="boss@campus.edu", role="admin")
        h = self.bearer(self.token_for(admin))
        for body in ({"role": "user"}, {"is_active": False}):
            r = self.client.patch(f"/admin/users/{admin.id}", headers=h, json=body)
            self.assertEqual(r.status_code, 400)

    def test_admin_deactivates_user_no_crash(self):
        """PATCH is_active=False must return the user, not 500 — the old code
        re-fetched via db.get() which filters inactive rows."""
        admin = self.make_user(email="boss@campus.edu", role="admin")
        user = self.make_user(email="target@campus.edu")
        h = self.bearer(self.token_for(admin))
        r = self.client.patch(f"/admin/users/{user.id}", headers=h, json={"is_active": False})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["user"]["is_active"])
        # reactivate an inactive user — the target must be findable too
        r = self.client.patch(f"/admin/users/{user.id}", headers=h, json={"is_active": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["user"]["is_active"])
        # role change on an inactive account must not 404
        self.client.patch(f"/admin/users/{user.id}", headers=h, json={"is_active": False})
        r = self.client.patch(f"/admin/users/{user.id}", headers=h, json={"role": "admin"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["user"]["role"], "admin")

    def test_service_token_is_admin(self):
        app = build(self.tmp / "svc", api_token="svc-token-123")
        c = TestClient(app, base_url="http://127.0.0.1")
        r = c.get("/metrics", headers=self.bearer("svc-token-123"))
        self.assertEqual(r.status_code, 200)

    # ── signup + guest tier ──────────────────────────────────────────────────

    def test_signup_creates_user_and_signs_in(self):
        r = self.client.post("/auth/signup",
                             json={"email": "new@campus.edu", "password": "my-password-1", "name": "New"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["user"]["role"], "user")
        self.assertEqual(self.domains(r.json()["token"]).status_code, 200)

    def test_signup_rejects_short_password_and_duplicates(self):
        r = self.client.post("/auth/signup", json={"email": "a@campus.edu", "password": "short"})
        self.assertEqual(r.status_code, 422)
        self.make_user(email="a@campus.edu")
        r = self.client.post("/auth/signup", json={"email": "a@campus.edu", "password": "long-enough-1"})
        self.assertEqual(r.status_code, 409)

    def guest(self):
        r = self.client.post("/auth/guest")
        self.assertEqual(r.status_code, 200)
        return r.json()["token"]

    def test_guest_can_query_paths_but_not_user_or_admin(self):
        token = self.guest()
        self.assertEqual(self.domains(token).status_code, 200)                    # guest-level
        self.assertEqual(self.client.get("/domains", headers=self.bearer(token)).status_code, 200)
        self.assertEqual(self.client.post("/feedback", headers=self.bearer(token),
                                          json={"request_id": "r", "resolved": True}).status_code, 403)
        self.assertEqual(self.client.get("/metrics", headers=self.bearer(token)).status_code, 403)

    def test_guest_query_rate_limit_per_ip(self):
        token = self.guest()
        codes = [self.client.post("/query", headers=self.bearer(token),
                                  json={"query": "q"}).status_code for _ in range(11)]
        self.assertEqual(codes[-1], 429)   # cap is 10/hour — the engine result doesn't matter

    def test_guest_pinned_to_general_domain(self):
        """Guest asking for it+hr still gets only ('general',) at the engine."""
        token = self.guest()
        self.client.post("/query", headers=self.bearer(token),
                         json={"query": "wifi?", "available": ["it", "hr"]})
        self.assertEqual(tuple(_Engine.last_request.available), ("general",))

    # ── per-account conversations ─────────────────────────────────────────────

    def conv_put(self, headers, sid, data=None):
        return self.client.put(f"/conversations/{sid}", headers=headers, json={
            "session_id": sid, "title": "t", "draft": "",
            "data": data if data is not None else {"id": sid, "messages": [{"role": "user", "text": "hi"}]}})

    def test_conversations_roundtrip(self):
        u = self.make_user()
        t = self.bearer(self.token_for(u))
        self.assertEqual(self.conv_put(t, "s1").status_code, 200)
        r = self.client.get("/conversations", headers=t)
        self.assertEqual([s["id"] for s in r.json()["sessions"]], ["s1"])

    def test_conversations_are_per_account(self):
        a, b = self.make_user(email="a@x.edu"), self.make_user(email="b@x.edu")
        self.conv_put(self.bearer(self.token_for(a)), "a-only")
        self.assertEqual(self.client.get("/conversations",
                                         headers=self.bearer(self.token_for(b))).json()["sessions"], [])

    def test_conversations_delete_and_guest_denied(self):
        u = self.make_user()
        t = self.bearer(self.token_for(u))
        self.conv_put(t, "s1")
        self.assertEqual(self.client.delete("/conversations/s1", headers=t).json()["deleted"], True)
        self.assertEqual(self.client.get("/conversations", headers=t).json()["sessions"], [])
        gt = self.guest()
        self.assertEqual(self.client.get("/conversations", headers=self.bearer(gt)).status_code, 403)

    # ── rate limit + bootstrap ────────────────────────────────────────────────

    def test_login_rate_limit(self):
        self.make_user()
        codes = [self.login("ada@campus.edu", "wrong-password").status_code for _ in range(9)]
        self.assertEqual(codes[-1], 429)
        self.assertTrue(all(c == 401 for c in codes[:-1]))

    def test_bootstrap_admin_from_env(self):
        os.environ["CORTEX_ADMIN_EMAIL"] = "root@campus.edu"
        os.environ["CORTEX_ADMIN_PASSWORD"] = "bootstrap-pass-1"
        try:
            app = build(self.tmp / "boot")
            c = TestClient(app, base_url="http://127.0.0.1")
            r = c.post("/auth/login", json={"email": "root@campus.edu", "password": "bootstrap-pass-1"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["user"]["role"], "admin")
            app.state.auth_db.close()
        finally:
            del os.environ["CORTEX_ADMIN_EMAIL"], os.environ["CORTEX_ADMIN_PASSWORD"]

    def test_short_secret_rejected_in_live(self):
        os.environ["CORTEX_JWT_SECRET"] = "short"
        try:
            with self.assertRaises(RuntimeError):
                auth.load_jwt_secret("live")
        finally:
            os.environ["CORTEX_JWT_SECRET"] = "test-secret-" + "x" * 40


if __name__ == "__main__":
    unittest.main()
