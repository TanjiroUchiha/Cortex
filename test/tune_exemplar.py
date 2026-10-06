"""Evaluate a second back-check signal: query vs per-domain exemplar queries.

The restatement check verifies a decision preserved the query's *content* — it
cannot catch a faithful-but-wrong-domain route. This checks *domain fit*:
cosine(query, exemplars_of_routed_domain). Run after tune_backcheck.py — it
reuses data/backcheck-tuning.json, so no LLM calls, embeddings only.

Usage: python tune_exemplar.py [--json data/backcheck-tuning.json]
"""
import argparse
import asyncio
import json
from pathlib import Path

from backend.store import OllamaEmbedder, cosine

ROOT = Path(__file__).resolve().parent

# few everyday questions a correct route for each domain should resemble
EXEMPLARS = {
    "it":         ["reset my portal password", "wifi keeps disconnecting on my laptop",
                   "install software on the lab computer", "my email login failed",
                   "the printer says paper jam"],
    "hr":         ["how do I apply for annual leave", "where can I download my payslip",
                   "my attendance record is wrong", "what is the relocation policy",
                   "update my emergency contact details"],
    "fees":       ["when is the tuition fee deadline", "pay my semester fees online",
                   "where is my fee receipt", "refund for excess payment",
                   "scholarship adjustment on my invoice"],
    "facilities": ["book a room for an event", "the fan in my hostel room is broken",
                   "the lift in the building is not working", "projector for my class",
                   "water leak in the corridor"],
    "general":    ["campus library opening hours", "where is the main help desk",
                   "official holiday list", "student clubs and events",
                   "how do I contact the administration"],
}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=str(ROOT.parent / "dataset" / "backcheck-tuning.json"))
    args = ap.parse_args()
    saved = json.loads(Path(args.json).read_text())
    rows = [r for r in (saved.get("rows") if isinstance(saved, dict) else saved)
            if r.get("action") == "route" and r.get("domains")]
    embedder = OllamaEmbedder()
    print("embedding exemplars…", flush=True)
    ex_vecs = {d: await asyncio.to_thread(embedder.batch, qs) for d, qs in EXEMPLARS.items()}
    print(f"scoring {len(rows)} routed queries…", flush=True)
    out = []
    for r in rows:
        qv = (await asyncio.to_thread(embedder.batch, [r["query"]]))[0]
        # worst-routed-domain exemplar sim — the gate is only as strong as its weakest domain
        sims = [max(cosine(qv, ev) for ev in ex_vecs[d]) for d in r["domains"] if d in ex_vecs]
        r["exemplar_sim"] = min(sims) if sims else None
        out.append(r)
        print(f"{r['id']:9s} ex={r['exemplar_sim'] and round(r['exemplar_sim'],3)} correct={r['correct']} :: {r['query'][:55]}", flush=True)
    ok = sorted(r["exemplar_sim"] for r in out if r["correct"] and r["exemplar_sim"] is not None)
    bad = sorted(r["exemplar_sim"] for r in out if not r["correct"] and r["exemplar_sim"] is not None)
    if ok:  print(f"\ncorrect: min={ok[0]:.3f} med={ok[len(ok)//2]:.3f} max={ok[-1]:.3f}")
    if bad: print(f"wrong:   min={bad[0]:.3f} med={bad[len(bad)//2]:.3f} max={bad[-1]:.3f}")
    # sweep: threshold = min exemplar sim a routed domain must clear
    best = None
    for t in range(30, 91):
        t /= 100
        ko = sum(1 for r in out if r["correct"] and (r["exemplar_sim"] or 0) >= t)
        db = sum(1 for r in out if not r["correct"] and (r["exemplar_sim"] or 0) < t)
        score = ko + db
        if best is None or score > best[1]:
            best = (t, score, ko, db)
    if best:
        print(f"best exemplar threshold {best[0]:.2f}: keeps {best[2]}/{len(ok)} correct, catches {best[3]}/{len(bad)} wrong")
        print(f"→ CORTEX_M1_EXEMPLAR_MIN={best[0]:.2f} (combine with the restatement check: demote if EITHER fails)")
    Path(args.json).write_text(json.dumps({"ran": "exemplar pass added", "rows": out}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
