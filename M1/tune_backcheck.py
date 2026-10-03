"""Tune the M1 back-check threshold (CORTEX_M1_BACKCHECK_MIN) on labeled data.

For each labeled query: run the real OllamaRouter (Prompt 1), take its route
decision, restate the question from that JSON alone (Prompt 2), then embed
query + restatement and record the cosine similarity. A decision is "correct"
when its domains equal the record's expected domains; the sweep below shows
how many correct decisions each threshold keeps vs wrongly demotes.

Usage:  python tune_backcheck.py [--split train,eval] [--limit N] [--out data/backcheck-tuning.json]
Requires Ollama (qwen3:4b + qwen3-embedding:0.6b). CPU-run: expect ~1-2 min/query.
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from m1 import Request
from services import OllamaRouter
from store import OllamaEmbedder, cosine

ROOT = Path(__file__).resolve().parent


def gold_domains(record):
    d = record["decision"]
    return (d.get("action"), tuple(sorted(t.get("domain") for t in d.get("tasks", []) if t.get("domain"))))


async def probe(records):
    embedder = OllamaEmbedder()
    router = OllamaRouter()  # backcheck off — we probe each decision manually
    out = []
    for i, rec in enumerate(records):
        q = rec["request"]["query"]
        request = Request(query=q, privacy=rec["request"].get("privacy", "local_only"),
                          available=tuple(rec["request"].get("available") or ["it", "hr", "fees", "facilities", "general"]),
                          clarify_attempts=rec["request"].get("clarify_attempts", 0))
        try:
            decision = await router(request)
        except Exception as exc:  # router unavailable → record and move on
            out.append({"id": rec["id"], "query": q, "error": str(exc)})
            continue
        if decision.get("action") != "route":
            out.append({"id": rec["id"], "query": q, "action": decision.get("action"),
                        "correct": decision.get("action") == rec["decision"].get("action"),
                        "similarity": None})
            continue
        restate = await router._restate(decision)
        sim = None
        if restate is not None:
            try:
                qv, rv = await asyncio.to_thread(embedder.batch, [q, restate])
                sim = cosine(qv, rv)
            except Exception:
                pass
        out.append({"id": rec["id"], "query": q,
                    "action": "route",
                    "domains": sorted(t.get("domain") for t in decision.get("tasks", [])),
                    "expected": sorted(rec["decision"].get("tasks") and gold_domains(rec)[1] or []),
                    "correct": sorted(t.get("domain") for t in decision.get("tasks", [])) == sorted(gold_domains(rec)[1]),
                    "restatement": restate, "similarity": sim})
        print(f"[{i+1}/{len(records)}] {rec['id']:9s} sim={sim if sim is None else round(sim,3)} "
              f"correct={out[-1]['correct']} :: {q[:60]}", flush=True)
    return out


def sweep(rows, lo=0.30, hi=0.90, step=0.01):
    """threshold = minimum similarity to keep a route. Score = kept-correct + dropped-wrong."""
    scored = [r for r in rows if r.get("action") == "route" and r.get("similarity") is not None]
    n_ok = sum(1 for r in scored if r["correct"])
    n_bad = len(scored) - n_ok
    best = None
    for t in range(int(lo * 100), int(hi * 100) + 1, int(step * 100)):
        t /= 100
        kept_ok = sum(1 for r in scored if r["correct"] and r["similarity"] >= t)
        dropped_bad = sum(1 for r in scored if not r["correct"] and r["similarity"] < t)
        score = kept_ok + dropped_bad
        if best is None or score > best[1]:
            best = (t, score, kept_ok, n_ok - kept_ok, dropped_bad, n_bad - dropped_bad)
    return scored, n_ok, n_bad, best


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="train,eval")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(ROOT / "data" / "backcheck-tuning.json"))
    args = ap.parse_args()
    splits = set(s.strip() for s in args.split.split(","))
    records = [r for r in json.load(open(ROOT / "data" / "starter.json")) if r.get("split") in splits]
    if args.limit:
        records = records[: args.limit]
    print(f"tuning on {len(records)} labeled records ({args.split}) — LLM x2 + 2 embeddings each", flush=True)
    started = time.time()
    rows = await probe(records)
    Path(args.out).write_text(json.dumps({"ran": time.strftime("%Y-%m-%d %H:%M"), "rows": rows}, indent=1))
    scored, n_ok, n_bad, best = sweep(rows)
    n_clar = sum(1 for r in rows if r.get("action") != "route" and not r.get("error"))
    n_err = sum(1 for r in rows if r.get("error"))
    print(f"\n=== results ({time.time()-started:.0f}s) ===")
    print(f"route decisions scored: {len(scored)}  (correct {n_ok} / wrong-domain {n_bad})   "
          f"clarify/other: {n_clar}   errors: {n_err}")
    if scored:
        sims_ok = sorted(r["similarity"] for r in scored if r["correct"])
        sims_bad = sorted(r["similarity"] for r in scored if not r["correct"])
        if sims_ok:  print(f"correct-route sims: min={sims_ok[0]:.3f} med={sims_ok[len(sims_ok)//2]:.3f} max={sims_ok[-1]:.3f}")
        if sims_bad: print(f"wrong-route  sims: min={sims_bad[0]:.3f} med={sims_bad[len(sims_bad)//2]:.3f} max={sims_bad[-1]:.3f}")
    if best:
        t, s, ko, ko_lost, db, db_kept = best
        print(f"best threshold {t:.2f}: keeps {ko}/{n_ok} correct, catches {db}/{n_bad} wrong  (score {s})")
        print(f"→ set CORTEX_M1_BACKCHECK_MIN={t:.2f}")
        print(f"   (below it: {ko_lost} correct decisions would be demoted to clarify; "
              f"{db_kept} wrong ones would still pass)")


if __name__ == "__main__":
    asyncio.run(main())
