"""Auth for Cortex: email/password sign-in, JWT bearer tokens, admin/user roles.

The account store is MongoDB Atlas (see ``backend/db.py``): normal users live
in ``users``, admins in ``admin_users``, chat history in ``conversations``.
JWTs are HS256, 60-minute bearer tokens; ``sub`` is the source of truth and
the role is re-resolved from the collections on every request — the token's
``role`` claim is cosmetic, so deactivation/demotion is immediate.
"""
from __future__ import annotations

from backend import obs
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request
from fastapi.security.utils import get_authorization_scheme_param

ISSUER = "cortex-m1"
TOKEN_TTL = 60 * 60          # 60 minutes, no refresh tokens
GUEST_TTL = 20 * 60          # guests get a short leash — 20 minutes
ROLE_LEVELS = {"guest": 0, "user": 1, "admin": 2}
MIN_SECRET = 32
MIN_PASSWORD = 10
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_hasher = PasswordHasher()
# Verify against a fixed dummy hash when the email is unknown so a missing
# account takes the same time as a real one (no user-enumeration timing oracle).
_DUMMY_HASH = _hasher.hash("timing-equalizer")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Principal:
    """An authenticated caller: a user record or the CORTEX_API_TOKEN service token."""
    id: str
    email: str
    name: str
    role: str                # "user" | "admin" | "guest"
    provider: str            # "local" | "service"

    def public(self) -> dict:
        return {"id": self.id, "email": self.email, "name": self.name,
                "role": self.role, "provider": self.provider}


def _public_doc(doc: dict, role: str) -> dict:
    """Store doc minus secrets/_id — the shape /admin/users and PATCH return."""
    return {"id": doc["id"], "email": doc["email"], "name": doc.get("name", ""),
            "role": role, "provider": doc.get("auth_provider", "local"),
            "is_active": bool(doc.get("is_active", True)),
            "created_at": doc.get("created_at", "")}


class AuthDB:
    """users + admin_users + conversations collections. Parameter-free filters
    only — every query is field equality, so nothing is ever interpolated."""

    def __init__(self, db):
        """`db` is a pymongo Database or anything dict-like that yields
        collections (``backend.db.MemoryDB`` in tests)."""
        self.db = db
        self.users = db["users"]
        self.admins = db["admin_users"]
        self.conversations = db["conversations"]

    @classmethod
    def for_env(cls):
        """Atlas store configured by MONGO_* env — raises a clear error when
        the credentials or the cluster are not reachable."""
        from backend import db as db_mod
        client, database = db_mod.connect()
        store = cls(database)
        store._client = client
        return store

    @classmethod
    def memory(cls):
        """In-memory store for tests and offline runs — no Atlas needed."""
        from backend.db import MemoryDB
        return cls(MemoryDB())

    def close(self):
        client = getattr(self, "_client", None)
        if client is not None:
            client.close()

    # ── lookups — role is derived from which collection holds the record ──────

    def _collection_for(self, role: str):
        return self.admins if role == "admin" else self.users

    def get(self, user_id) -> Principal | None:
        doc = self.users.find_one({"id": user_id})
        role = "user"
        if doc is None:
            doc = self.admins.find_one({"id": user_id})
            role = "admin"
        if not doc or not doc.get("is_active", True):
            return None
        return Principal(doc["id"], doc["email"], doc.get("name", ""),
                         role, doc.get("auth_provider", "local"))

    def get_record(self, user_id) -> dict | None:
        """Public-shape record incl. is_active — /admin/users reads/patches."""
        doc = self.users.find_one({"id": user_id})
        if doc is not None:
            return _public_doc(doc, "user")
        doc = self.admins.find_one({"id": user_id})
        return _public_doc(doc, "admin") if doc is not None else None

    def get_by_email(self, email) -> dict | None:
        """Full record for credential checks — includes password_hash and the
        role derived from the collection it was found in."""
        email = email.strip().lower()
        doc = self.users.find_one({"email": email})
        role = "user"
        if doc is None:
            doc = self.admins.find_one({"email": email})
            role = "admin"
        if doc is None:
            return None
        doc = {k: v for k, v in doc.items() if k != "_id"}
        doc["role"] = role
        return doc

    def list_users(self) -> list[dict]:
        rows = [_public_doc(d, "user") for d in self.users.find({})]
        rows += [_public_doc(d, "admin") for d in self.admins.find({})]
        return sorted(rows, key=lambda r: r.get("created_at") or "")

    # ── writes ────────────────────────────────────────────────────────────────

    def create(self, email, name, password, role="user", provider="local") -> Principal:
        email = email.strip().lower()
        if not EMAIL_RE.match(email):
            raise ValueError("Invalid email")
        if role not in ("user", "admin"):
            raise ValueError("Role must be user or admin")
        if provider == "local" and (not password or len(password) < MIN_PASSWORD):
            raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
        if self.get_by_email(email):
            raise ValueError("An account with that email already exists")
        doc = {"id": uuid.uuid4().hex, "email": email, "name": (name or "").strip(),
               "password_hash": _hasher.hash(password) if password else None,
               "auth_provider": provider, "is_active": True, "created_at": _now()}
        self._collection_for(role).insert_one(doc)
        return Principal(doc["id"], email, doc["name"], role, provider)

    def set_role(self, user_id, role):
        """Demote/promote = move the record between collections, so role is a
        fact about where the record lives, not a mutable field."""
        if role not in ("user", "admin"):
            raise ValueError("Role must be user or admin")
        for source, source_role in ((self.users, "user"), (self.admins, "admin")):
            doc = source.find_one({"id": user_id})
            if doc is not None:
                if source_role != role:
                    source.delete_one({"id": user_id})
                    self._collection_for(role).insert_one(doc)
                return
        raise ValueError("No such user")

    def set_active(self, user_id, active: bool):
        self._update_existing(user_id, {"is_active": bool(active)})

    def set_password(self, user_id, password):
        if not password or len(password) < MIN_PASSWORD:
            raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
        self._update_existing(user_id, {"password_hash": _hasher.hash(password)})

    def _update_existing(self, user_id, fields: dict):
        for coll in (self.users, self.admins):
            if coll.update_one({"id": user_id}, {"$set": fields}).matched_count:
                return
        raise ValueError("No such user")

    def has_admin(self) -> bool:
        return self.admins.count_documents({"is_active": True}) > 0

    # ── per-account conversation persistence ─────────────────────────────────
    # Docs are keyed (user_id, session_id) — one account can never read or
    # overwrite another's history; `data` holds the whole session descriptor.

    def list_conversations(self, user_id) -> list[dict]:
        import json
        rows = self.conversations.find({"user_id": user_id})
        rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
        return [json.loads(r["data"]) for r in rows]

    def put_conversation(self, user_id, session_id, title, draft, data):
        self.conversations.update_one(
            {"user_id": user_id, "session_id": session_id},
            {"$set": {"title": title, "draft": draft, "data": data,
                      "updated_at": _now()}},
            upsert=True)

    def delete_conversation(self, user_id, session_id) -> bool:
        return bool(self.conversations.delete_one(
            {"user_id": user_id, "session_id": session_id}).deleted_count)


def verify_password(row: dict | None, password: str) -> bool:
    """Constant-time-ish check: unknown emails still cost an argon2 verify."""
    hashed = row["password_hash"] if row and row.get("password_hash") else _DUMMY_HASH
    try:
        ok = _hasher.verify(hashed, password or "")
    except (VerifyMismatchError, VerificationError, Argon2Error):
        ok = False
    return ok and bool(row and row.get("is_active", True))


def load_jwt_secret(mode: str) -> str:
    secret = os.environ.get("CORTEX_JWT_SECRET", "")
    if len(secret) >= MIN_SECRET:
        return secret
    if mode == "live":
        raise RuntimeError(
            "CORTEX_JWT_SECRET (>=32 chars) is required in live mode — "
            "e.g. $env:CORTEX_JWT_SECRET = python -c 'import secrets; print(secrets.token_hex(32))'")
    if secret:
        print("auth: CORTEX_JWT_SECRET is set but shorter than 32 chars — ignoring it")
    generated = secrets.token_hex(32)
    print("auth: CORTEX_JWT_SECRET unset — using an ephemeral dev key; "
          "all logins die when the server restarts. Set a stable secret for shared use.")
    return generated


def issue_jwt(user: Principal, secret: str) -> str:
    now = int(time.time())
    return jwt.encode({"sub": user.id, "role": user.role, "email": user.email,
                       "iat": now, "exp": now + TOKEN_TTL, "iss": ISSUER},
                      secret, algorithm="HS256")


def issue_guest_jwt(secret: str) -> str:
    """Guests are stateless tokens — no DB row, short expiry, minimal role.
    Their sub is unique per issuance but carries no account to demote."""
    now = int(time.time())
    return jwt.encode({"sub": f"guest-{uuid.uuid4().hex[:12]}", "role": "guest",
                       "iat": now, "exp": now + GUEST_TTL, "iss": ISSUER},
                      secret, algorithm="HS256")


def decode_jwt(token: str, secret: str) -> dict:
    """Strict decode: HS256 only, fixed issuer, required claims. Raises on anything odd."""
    return jwt.decode(token, secret, algorithms=["HS256"], issuer=ISSUER,
                      options={"require": ["sub", "iat", "exp", "iss"],
                               "verify_signature": True, "verify_exp": True,
                               "verify_iat": True, "verify_iss": True})


class RateLimiter:
    """In-memory sliding-window limiter. Per-process only — resets on restart."""
    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: dict[str, list[float]] = {}

    def check(self, key: str):
        now = time.monotonic()
        hits = [t for t in self.hits.get(key, []) if now - t < self.window]
        if len(hits) >= self.limit:
            self.hits[key] = hits
            raise HTTPException(429, "Too many attempts — wait a minute and try again")
        hits.append(now)
        self.hits[key] = hits


# ── FastAPI dependencies ──────────────────────────────────────────────────────

def _service_principal() -> Principal:
    return Principal("service:api-token", "", "API token", "admin", "service")


async def get_principal(request: Request) -> Principal | None:
    """Resolve the caller: CORTEX_API_TOKEN service bearer, or a JWT whose sub
    is a live user record (role comes from the collections, never the token)."""
    header = request.headers.get("authorization", "")
    scheme, credentials = get_authorization_scheme_param(header)
    if scheme.lower() != "bearer" or not credentials:
        return None
    api_token = getattr(request.app.state, "api_token", None)
    if api_token and secrets.compare_digest(credentials.encode(), api_token.encode()):
        return _service_principal()
    try:
        claims = decode_jwt(credentials, request.app.state.jwt_secret)
    except jwt.PyJWTError:
        return None
    if claims.get("role") == "guest":
        return Principal(claims["sub"], "", "Guest", "guest", "local")
    return request.app.state.auth_db.get(claims["sub"])


async def require_user(request: Request) -> Principal:
    principal = await get_principal(request)
    if principal is None:
        obs.auth_rejected("missing_or_invalid_token", 401, path=request.url.path)
        raise HTTPException(401, "Sign in required")
    obs.set_user(principal.id, principal.role)
    return principal


def require_role(minimum: str):
    """Level-based check: admin satisfies user-level routes, user satisfies guest
    routes. Guests are excluded from anything user-level or above."""
    async def dep(request: Request) -> Principal:
        principal = await get_principal(request)
        if principal is None:
            obs.auth_rejected("missing_or_invalid_token", 401, path=request.url.path)
            raise HTTPException(401, "Sign in required")
        if ROLE_LEVELS.get(principal.role, -1) < ROLE_LEVELS[minimum]:
            obs.auth_rejected(f"below_role:{minimum}", 403,
                              path=request.url.path, role=principal.role)
            raise HTTPException(403, f"{minimum.capitalize()} access required")
        obs.set_user(principal.id, principal.role)
        return principal
    return dep


require_admin = Depends(require_role("admin"))
