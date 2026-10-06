"""Document-level access control — maps corpus governance metadata onto the
app's account tiers (guest < user < admin) and is enforced at retrieval time,
in the /corpus listing, and in file downloads.

The corpus carries `sensitivity`, `allowed_roles`, `allowed_users` and
`allowed_entities` frontmatter fields. This module turns them into a minimum
app tier per document:

  sensitivity        → floor: PUBLIC guest, INTERNAL user,
                       CONFIDENTIAL/RESTRICTED admin
  allowed_roles      → floor: a broad institutional role (STUDENT/FACULTY/
                       STAFF/EMPLOYEE) means any signed-in member qualifies;
                       only privileged departmental roles listed (HR, FINANCE,
                       IT, MANAGER, ADMIN...) requires admin — app accounts do
                       not carry department membership, so we cannot verify it.
  allowed_users /    → admin only: these name specific people/entities and no
  allowed_entities     app account can be matched to them.

Effective minimum tier = the strictest of the three (most restrictive wins).
"""
from __future__ import annotations

TIERS = {"guest": 0, "user": 1, "admin": 2}

BROAD_ROLES = {"STUDENT", "FACULTY", "STAFF", "EMPLOYEE"}

SENSITIVITY_TIER = {"PUBLIC": "guest", "INTERNAL": "user",
                    "CONFIDENTIAL": "admin", "RESTRICTED": "admin"}


def required_role(metadata: dict) -> str:
    """Minimum app tier needed to see this document."""
    meta = metadata or {}
    if meta.get("allowed_users") or meta.get("allowed_entities"):
        return "admin"
    sensitivity = str(meta.get("sensitivity") or "INTERNAL").upper()
    tier = SENSITIVITY_TIER.get(sensitivity, "user")   # unknown label -> member docs
    roles = {str(r).upper() for r in (meta.get("allowed_roles") or [])}
    if roles and not (roles & BROAD_ROLES):
        tier = "admin"                               # privileged-roles-only ACL
    return tier


def can_view(metadata: dict, viewer_role: str) -> bool:
    """True when a principal at `viewer_role` may read a doc with `metadata`."""
    return TIERS.get(viewer_role, 0) >= TIERS.get(required_role(metadata), 2)
