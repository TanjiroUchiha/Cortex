"""MongoDB Atlas account store for Cortex.

``MONGO_USERNAME`` / ``MONGO_PASSWORD`` / ``MONGO_CLUSTER`` (and optionally
``MONGO_DB``, default ``cortex``) build a ``mongodb+srv://`` URI - the values
live in ``.env`` locally and are never committed.

Collections:

  users          - normal accounts (role "user")
  admin_users    - admin accounts  (role "admin")
  documents      - upload provenance for the shared corpus
  conversations  - per-account chat sessions

Role is derived from which collection holds the record - never from a stored
field or the JWT claim - so demoting an admin is a move between collections
and a deleted account loses access on the next request.

``MemoryDB`` is an in-memory stand-in with the pymongo surface AuthDB uses.
Tests and scripts inject it; the real app always goes through ``connect()``.
"""
from __future__ import annotations

import os
import time
from types import SimpleNamespace
from urllib.parse import quote_plus

from dotenv import load_dotenv
from pymongo import ASCENDING, MongoClient
from pymongo.errors import PyMongoError

load_dotenv()

REQUIRED_ENV = ("MONGO_USERNAME", "MONGO_PASSWORD", "MONGO_CLUSTER")
DEFAULT_DB = "cortex"


def mongo_uri() -> str:
    """Atlas SRV URI with URL-quoted credentials; clear error when unset."""
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k, "").strip()]
    if missing:
        raise RuntimeError(
            "MongoDB Atlas is not configured - set " + ", ".join(missing) +
            " in .env (MONGO_USERNAME, MONGO_PASSWORD, and MONGO_CLUSTER like "
            "cluster0.xxxxx.mongodb.net).")
    user = quote_plus(os.environ["MONGO_USERNAME"])
    password = quote_plus(os.environ["MONGO_PASSWORD"])
    cluster = os.environ["MONGO_CLUSTER"].strip().rstrip("/")
    return f"mongodb+srv://{user}:{password}@{cluster}/"


def connect(ping: bool = True, retries: int = 3):
    """(client, db) with a fast-fail server selection timeout and index setup.
    The initial handshake is retried — shared Atlas tiers (M0) intermittently
    reject TLS handshakes, and a single-shot connect would flake startup."""
    last_exc = None
    for attempt in range(retries):
        try:
            client = MongoClient(mongo_uri(), serverSelectionTimeoutMS=5000)
            if ping:
                client.admin.command("ping")
            db = client[os.environ.get("MONGO_DB", DEFAULT_DB).strip() or DEFAULT_DB]
            ensure_indexes(db)
            return client, db
        except PyMongoError as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(1.5)
    raise RuntimeError(
        "Could not reach MongoDB Atlas - check MONGO_CLUSTER, the "
        "database user's credentials, and Atlas > Network Access "
        "(your IP must be allowlisted).") from last_exc


def ensure_indexes(db):
    """Idempotent startup indexes - uniqueness rules the app relies on."""
    db["users"].create_index("email", unique=True)
    db["admin_users"].create_index("email", unique=True)
    db["documents"].create_index("doc_id", unique=True)
    db["conversations"].create_index(
        [("user_id", ASCENDING), ("session_id", ASCENDING)], unique=True)


# ── in-memory stand-in (tests / offline use) ─────────────────────────────────
# Implements only the pymongo surface AuthDB and the routes touch: equality
# filters, $set updates, upserts, counts. Unique indexes are enforced on the
# fields ensure_indexes() declares.

class MemoryCollection:
    _UNIQUE = {"users": {"email"}, "admin_users": {"email"},
               "documents": {"doc_id"},
               "conversations": {("user_id", "session_id")}}

    def __init__(self, name):
        self.name = name
        self.rows: list[dict] = []

    @staticmethod
    def _match(row, flt):
        return all(row.get(k) == v for k, v in flt.items())

    def _check_unique(self, doc):
        for keys in self._UNIQUE.get(self.name, ()):
            keys = (keys,) if isinstance(keys, str) else keys
            if all(k in doc for k in keys) and any(
                    all(r.get(k) == doc[k] for k in keys) for r in self.rows):
                raise ValueError(f"duplicate key on {self.name}.{keys}")

    def find_one(self, flt, projection=None):
        return next((dict(r) for r in self.rows if self._match(r, flt)), None)

    def find(self, flt=None, projection=None):
        return [dict(r) for r in self.rows if self._match(r, flt or {})]

    def insert_one(self, doc):
        self._check_unique(doc)
        self.rows.append(dict(doc))
        return SimpleNamespace(inserted_id=doc.get("_id"))

    def update_one(self, flt, update, upsert=False):
        for r in self.rows:
            if self._match(r, flt):
                r.update(update.get("$set", {}))
                return SimpleNamespace(matched_count=1, modified_count=1)
        if upsert:
            doc = {**flt, **update.get("$set", {})}
            self._check_unique(doc)
            self.rows.append(doc)
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)
        return SimpleNamespace(matched_count=0, modified_count=0)

    def replace_one(self, flt, doc, upsert=False):
        for i, r in enumerate(self.rows):
            if self._match(r, flt):
                self.rows[i] = dict(doc)
                return SimpleNamespace(matched_count=1, modified_count=1)
        if upsert:
            self.rows.append(dict(doc))
        return SimpleNamespace(matched_count=0, modified_count=0)

    def delete_one(self, flt):
        for i, r in enumerate(self.rows):
            if self._match(r, flt):
                del self.rows[i]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    def count_documents(self, flt):
        return sum(1 for r in self.rows if self._match(r, flt))

    def create_index(self, *args, **kwargs):
        return "idx"


class MemoryDB:
    """Dict-like database: db["users"] -> MemoryCollection, same as pymongo."""

    def __init__(self):
        self._cols: dict[str, MemoryCollection] = {}

    def __getitem__(self, name) -> MemoryCollection:
        return self._cols.setdefault(name, MemoryCollection(name))
