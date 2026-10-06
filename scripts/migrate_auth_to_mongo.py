"""One-off migration: legacy SQLite account store -> MongoDB Atlas.

Reads the old ``backend/data/auth.db`` (or ``--db <path>``) and copies:
  users.role = 'user'  -> users          collection
  users.role = 'admin' -> admin_users    collection
  conversations        -> conversations  collection

Already-hashed argon2 passwords are carried over verbatim — users keep
their passwords. Records already present in Atlas (same ``id``) are skipped,
so the script is safe to re-run.

Usage:
    python scripts/migrate_auth_to_mongo.py [--db backend/data/auth.db]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import db as db_mod  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(
        Path(__file__).resolve().parent.parent / "backend" / "data" / "auth.db"))
    args = parser.parse_args()

    src = sqlite3.connect(args.db)
    src.row_factory = sqlite3.Row
    client, mongo = db_mod.connect()

    def rows(table):
        try:
            return src.execute(f"SELECT * FROM {table}").fetchall()
        except sqlite3.OperationalError:
            return []

    moved = skipped = 0
    for row in rows("users"):
        coll = mongo["admin_users" if row["role"] == "admin" else "users"]
        doc = {"id": row["id"], "email": row["email"], "name": row["name"],
               "password_hash": row["password_hash"],
               "auth_provider": row["auth_provider"],
               "is_active": bool(row["is_active"]),
               "created_at": row["created_at"]}
        if coll.find_one({"id": doc["id"]}):
            skipped += 1
            continue
        coll.insert_one(doc)
        moved += 1
    print(f"users: {moved} migrated, {skipped} already present")

    moved = skipped = 0
    for row in rows("conversations"):
        flt = {"user_id": row["user_id"], "session_id": row["session_id"]}
        if mongo["conversations"].find_one(flt):
            skipped += 1
            continue
        mongo["conversations"].insert_one(
            {**flt, "title": row["title"], "draft": row["draft"],
             "data": row["data"], "updated_at": row["updated_at"]})
        moved += 1
    print(f"conversations: {moved} migrated, {skipped} already present")
    client.close()


if __name__ == "__main__":
    main()
