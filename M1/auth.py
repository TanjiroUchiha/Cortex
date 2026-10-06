"""Auth for Cortex: local email/password sign-in, JWT bearer tokens, admin/user roles.

One SQLite file (``data/auth.db``) is the account store — the project's only DB.
JWTs are HS256, 60-minute bearer tokens; ``sub`` is the source of truth and the
role is re-read from the DB on every request, so deactivation/demotion is
immediate and a forged ``role`` claim does nothing.
"""
from __future__ import annotations

import obs
import os
import re
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

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


@dataclass
class Principal:
    """An authenticated caller: a user row or the CORTEX_API_TOKEN service token."""
    id: str
    email: str
    name: str
    role: str                # "user" | "admin"
    provider: str            # "local" | "service"

    def public(self) -> dict:
        return {"id": self.id, "email": self.email, "name": self.name,
                "role": self.role, "provider": self.provider}


class AuthDB:
    """users table under data/auth.db — parameterized queries only."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("""CREATE TABLE IF NOT EXISTS users (
            id            TEXT PRIMARY KEY,
            email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
            name          TEXT NOT NULL DEFAULT '',
            password_hash TEXT,
            role          TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user','admin')),
            auth_provider TEXT NOT NULL DEFAULT 'local'
                          CHECK(auth_provider IN ('local','google','local+google')),
            is_active     INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS conversations (
            user_id    TEXT NOT NULL,
            session_id TEXT NOT NULL,
            title      TEXT,
            draft      TEXT,
            data       TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, session_id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )""")
        self.db.commit()

    def close(self):
        self.db.close()

    @staticmethod
    def _row(row) -> Principal | None:
        if row is None:
            return None
        return Principal(row["id"], row["email"], row["name"],
                         row["role"], row["auth_provider"])

    def get(self, user_id) -> Principal | None:
        row = self.db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._row(row) if row and row["is_active"] else None

    def get_by_email(self, email) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()

    def list_users(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, email, name, role, auth_provider, is_active, created_at "
            "FROM users ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def create(self, email, name, password, role="user", provider="local") -> Principal:
        email = email.strip().lower()
        if not EMAIL_RE.match(email):
            raise ValueError("Invalid email")
        if role not in ("user", "admin"):
            raise ValueError("Role must be user or admin")
        if provider == "local":
            if not password or len(password) < MIN_PASSWORD:
                raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
            password_hash = _hasher.hash(password)
        else:
            password_hash = _hasher.hash(password) if password else None
        uid = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO users (id, email, name, password_hash, role, auth_provider) "
            "VALUES (?,?,?,?,?,?)", (uid, email, (name or "").strip(), password_hash, role, provider))
        self.db.commit()
        return Principal(uid, email, (name or "").strip(), role, provider)

    def set_role(self, user_id, role):
        if role not in ("user", "admin"):
            raise ValueError("Role must be user or admin")
        self.db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        self.db.commit()

    def set_active(self, user_id, active: bool):
        self.db.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if active else 0, user_id))
        self.db.commit()

    def set_password(self, user_id, password):
        if not password or len(password) < MIN_PASSWORD:
            raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
        self.db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                        (_hasher.hash(password), user_id))
        self.db.commit()

    def has_admin(self) -> bool:
        return bool(self.db.execute(
            "SELECT 1 FROM users WHERE role = 'admin' AND is_active = 1").fetchone())

    # ── per-account conversation persistence ─────────────────────────────────
    # Rows are keyed (user_id, session_id) — one account can never read or
    # overwrite another's history; `data` holds the whole session descriptor.

    def list_conversations(self, user_id) -> list[dict]:
        rows = self.db.execute(
            "SELECT data FROM conversations WHERE user_id = ? ORDER BY updated_at DESC, rowid DESC",
            (user_id,)).fetchall()
        import json
        return [json.loads(r["data"]) for r in rows]

    def put_conversation(self, user_id, session_id, title, draft, data):
        self.db.execute(
            "INSERT INTO conversations (user_id, session_id, title, draft, data, updated_at) "
            "VALUES (?,?,?,?,?, datetime('now')) "
            "ON CONFLICT(user_id, session_id) DO UPDATE SET "
            "title=excluded.title, draft=excluded.draft, data=excluded.data, "
            "updated_at=excluded.updated_at",
            (user_id, session_id, title, draft, data))
        self.db.commit()

    def delete_conversation(self, user_id, session_id) -> bool:
        cur = self.db.execute(
            "DELETE FROM conversations WHERE user_id = ? AND session_id = ?",
            (user_id, session_id))
        self.db.commit()
        return cur.rowcount > 0


def verify_password(row: sqlite3.Row | None, password: str) -> bool:
    """Constant-time-ish check: unknown emails still cost an argon2 verify."""
    hashed = row["password_hash"] if row and row["password_hash"] else _DUMMY_HASH
    try:
        ok = _hasher.verify(hashed, password or "")
    except (VerifyMismatchError, VerificationError, Argon2Error):
        ok = False
    return ok and bool(row and row["is_active"])


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
    is a live user row (role comes from the DB, never the token)."""
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
