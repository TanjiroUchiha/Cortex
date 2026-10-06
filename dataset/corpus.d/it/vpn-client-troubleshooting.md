---
document_id: IT-VPN-CLIENT-TROUBLESHOOTING
path: it/vpn-client-troubleshooting.md
title: VPN client troubleshooting
category: it
department: Information Technology
document_type: Eligibility rule
sensitivity: INTERNAL
allowed_roles:
- STUDENT
- FACULTY
- STAFF
- EMPLOYEE
- MANAGER
- ADMIN
- SUPER_ADMIN
allowed_users: []
allowed_entities: []
entity: Campus
version: '1.0'
effective_date: '2026-07-01'
status: active
superseded_date: null
supersedes: null
superseded_by: null
origin: authored synthetic corpus record
preserved_seed_sources: []
related_documents: []
access_level: Internal
synthetic: true
package: expanded-corpus
---

# VPN client troubleshooting

## Summary
For a VPN client that will not connect, the IT checklist is: confirm the campus credentials work on the portal, complete the MFA prompt, check the device clock is within one minute of network time, then reinstall the current VPN client version from the IT portal. Most failures resolve at the MFA or version step.

## Details
On managed laptops the client updates automatically; personal devices download it from the IT portal. Connection drops inside campus networks are expected — VPN is for off-campus use and is blocked on the campus WiFi itself. Persistent failures go to the IT Helpdesk (it-help@cdu.example, ext 4357) with the client version and exact error text.

## Notes
A locked account (after repeated MFA failures) is unlocked at the Helpdesk; the VPN error message does not distinguish it from a wrong password.
