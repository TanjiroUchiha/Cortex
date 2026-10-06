"""Run the expanded-corpus evaluation questions against the real pipeline.

Usage: python -m test.evaluate_corpus [keyword|semantic] [--role guest|user|admin]

Scores each question by the citations the pipeline actually produced, mapped to
package relative paths. `--role` runs every question at that access tier —
enforcement is real (backend/access.py + retrieval filtering), so governance
questions measure actual access control now, not just coverage.
"""
import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT.parent / "dataset" / "evaluation"
sys.path.insert(0, str(ROOT.parent))

from models.m1 import Request
from backend.orchestrator import Orchestrator
from backend.services import KeywordRouter, demo_services
from backend.store import CorpusIndex, load_documents

ABSTAIN_STATUSES = {"unsupported", "clarify", "handoff", "no_evidence",
                    "routing_failed", "deadline_exceeded", "busy"}


def load_questions():
    groups = {}
    for name in ("retrieval-questions", "governance-questions",
                 "multi-document-questions", "negative-questions"):
        path = PACKAGE / f"{name}.json"
        items = json.loads(path.read_text(encoding="utf-8"))
        groups[name] = items["questions"] if isinstance(items, dict) else items
    return groups


async def run_eval(retrieval="keyword", role="admin"):
    documents = load_documents(ROOT.parent / "dataset" / "corpus.json")
    corpus = CorpusIndex(documents)
    if retrieval == "semantic":
        from backend.store import SemanticIndex
        corpus = SemanticIndex(documents)   # needs Ollama + embedding model
    paths = {doc_id: s.get("relative_path")
             for body in corpus.documents.values() for doc_id, s in body["sources"].items()}
    engine = Orchestrator(KeywordRouter(corpus), demo_services(corpus),
                          scorer=corpus.scores, domain_titles=corpus.titles())

    groups = load_questions()
    report = {"role": role, "total": 0, "groups": {}}
    misses = []

    for name, questions in groups.items():
        stats = Counter()
        for q in questions:
            result = await engine.run(Request(query=q["question"], viewer_role=role))
            cited = {paths.get(c["doc_id"], c["doc_id"]) for c in result["citations"]}
            expected = set(q.get("expected_sources", []))
            stats["n"] += 1
            if name == "negative-questions":
                ok = result["status"] in ABSTAIN_STATUSES or not result["response"]
                stats["abstained" if ok else "answered_anyway"] += 1
                if not ok:
                    misses.append((name, q["question"], result["status"]))
            else:
                covered = expected & cited
                stats["routed_" + result["status"]] += 1
                if result["status"] == "completed" and covered == expected and expected:
                    stats["verified_full_coverage"] += 1
                stats["full" if covered == expected and expected else
                      ("partial" if covered else "miss")] += 1
                if covered != expected:
                    misses.append((name, q["question"],
                                   sorted(expected - cited), result["status"]))
            report["total"] += 1
        report["groups"][name] = dict(stats)

    print(json.dumps(report, indent=2))
    print(f"\nNOTE: ran at viewer role '{role}' — access enforcement is active; "
          "docs above the tier are invisible to retrieval.")
    if misses:
        print(f"\n{len(misses)} misses:")
        for m in misses[:20]:
            print("  -", m)
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate corpus retrieval and verified answer coverage.")
    parser.add_argument("retrieval", nargs="?", choices=("keyword", "semantic"), default="keyword")
    parser.add_argument("--role", choices=("guest", "user", "admin"), default="admin")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(run_eval(args.retrieval, args.role))
