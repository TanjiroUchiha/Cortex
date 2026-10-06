"""Seed an admin account into MongoDB Atlas (the ``admin_users`` collection).

Usage:
    python scripts/create_admin.py                 # prompts for email + password
    python scripts/create_admin.py admin@campus.edu

Reads MONGO_USERNAME / MONGO_PASSWORD / MONGO_CLUSTER from .env (or the
environment). Passwords are argon2-hashed by the same hasher the app uses;
an existing email is updated (re-activated + password reset) only when
``--reset`` is passed, never silently.
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.auth import AuthDB, MIN_PASSWORD  # noqa: E402


def main(argv):
    reset = "--reset" in argv
    argv = [a for a in argv if a != "--reset"]
    email = (argv[0] if argv else input("Admin email: ")).strip().lower()
    password = getpass.getpass("Password: ") or ""
    if len(password) < MIN_PASSWORD:
        sys.exit(f"Password must be at least {MIN_PASSWORD} characters")
    if getpass.getpass("Repeat password: ") != password:
        sys.exit("Passwords do not match")

    db = AuthDB.for_env()
    existing = db.get_by_email(email)
    if existing and not reset:
        sys.exit(f"{email} already exists as role '{existing['role']}' — "
                 "pass --reset to rotate its password and re-activate it.")
    if existing:
        db.set_password(existing["id"], password)
        db.set_active(existing["id"], True)
        if existing["role"] != "admin":
            db.set_role(existing["id"], "admin")
        print(f"admin {email} updated (password reset, active, role=admin)")
    else:
        db.create(email, name="Admin", password=password, role="admin")
        print(f"admin {email} created in admin_users")
    db.close()


if __name__ == "__main__":
    main(sys.argv[1:])
