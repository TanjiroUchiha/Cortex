"""Batch-test V1 against the running Cortex API.
Usage (server already running):  cd M1 && python run_queries.py
Needs `result["v1_verdict"] = verification` in orchestrator.py (line after the v1 emit)."""
import json, urllib.request

URL = "http://127.0.0.1:8000/query"

# (query, what I expect from V1)
CASES = [
    # --- single domain, how-to (expect passed) ---
    ("How do I reset my password?",                         "passed"),
    ("How do I connect to the campus Wi-Fi?",               "passed"),
    ("How do I apply for leave?",                           "passed"),
    ("How do I book a seminar room?",                       "passed"),
    ("How do I get a bonafide certificate?",                "passed"),
    # --- asks for a date/number (expect uncertain unless corpus has a real one) ---
    ("What is the last date to pay semester fees?",         "uncertain/incomplete_answer unless a date is in the corpus"),
    ("When is the exam registration deadline?",             "uncertain/incomplete_answer unless a date is in the corpus"),
    ("How much is the late payment fine?",                  "uncertain/incomplete_answer unless an amount is in the corpus"),
    ("What are the gym opening hours?",                     "passed if hours are in corpus"),
    # --- contact questions ---
    ("Who do I contact about a fee refund?",                "passed (contact present)"),
    ("What is the IT helpdesk email?",                      "passed if an email is in corpus"),
    # --- two topics in one chat (watch for missing_domain) ---
    ("How do I reset my password and apply for leave?",     "passed; missing_domain = M2 dropped half"),
    ("Wi-Fi not working and I need a fee receipt",          "passed; missing_domain = M2 dropped half"),
    ("Fee deadline and gym timings",                        "uncertain (fee date) but no missing_domain"),
    # --- vague / off-corpus (expect clarify, no_evidence, or unsupported - V1 may not run) ---
    ("Can I bring a pet to my hostel room?",                "no_evidence (V1 should not run)"),
    ("What is the capital of France?",                      "unsupported (V1 should not run)"),
    ("I lost my card",                                      "clarify/handoff (ambiguous)"),
    ("hello",                                               "smalltalk (V1 should not run)"),
]


def ask(q):
    req = urllib.request.Request(URL, data=json.dumps({"query": q}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=130) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")
    except Exception as e:
        return {"status": f"ERROR {e}"}


import sys, textwrap

# Usage:  python run_queries.py                -> all built-in cases (short draft)
#         python run_queries.py "my question"  -> just those questions, FULL draft + evidence
custom = sys.argv[1:]
cases = [(q, "") for q in custom] if custom else CASES

for q, expect in cases:
    res = ask(q)
    v = res.get("v1_verdict") or {}
    domains = ",".join((res.get("routing") or {}).get("domains", [])) or "-"
    print(f"\nQ: {q}")
    print(f"   pipeline: {res.get('status')} | domains: {domains}")
    if v:
        print(f"   V1: {v.get('status')} {v.get('flags')}")
        print(f"       {v.get('explanation')}")
    else:
        print("   V1: (did not run)")
    draft = " ".join((res.get("draft") or res.get("message") or "").split())
    if custom:
        print("   FULL DRAFT:")
        print(textwrap.indent(textwrap.fill(draft, 110), "     "))
        for sk in res.get("skills", []):
            print(f"   evidence [{sk['domain']}]: {[e['doc_id'] for e in sk.get('evidence', [])]}")
    else:
        print(f"   draft: {draft[:260]}{'...' if len(draft) > 260 else ''}")
    if expect:
        print(f"   expected: {expect}")