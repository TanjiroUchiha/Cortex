import argparse
import hashlib
import json
import time
from pathlib import Path

from dataset import DEFAULT_DATA, load_records
from m1 import DOMAINS, SYSTEM_PROMPT, ambiguous_item_options, ollama_route, parse_decision, request_from_dict, unique_object


def score_predictions(records, predictions, split="eval"):
    if split not in ("eval", "test", "heldout"):
        raise ValueError("Only eval, test or heldout splits can be scored")
    records = [row for row in records if row["split"] == split]
    if not records or not isinstance(predictions, dict):
        raise ValueError("Evaluation requires held-out records and an ID-to-output object")
    unknown = set(predictions) - {row["id"] for row in records}
    if unknown:
        raise ValueError("Predictions contain unknown or training IDs")
    valid = actions = routes = 0
    per_role = {name: {"tp": 0, "fp": 0, "fn": 0} for name in DOMAINS}
    for row in records:
        expected = row["decision"]
        expected_set = {t["domain"] for t in expected["tasks"]}
        actual_set = set()
        try:
            actual = parse_decision(predictions.get(row["id"], ""), request_from_dict(row["request"]))
        except (ValueError, TypeError):
            actual = None
        if actual is not None:
            valid += 1
            actual_set = {t["domain"] for t in actual["tasks"]}
            correct_action = actual["action"] == expected["action"]
            actions += correct_action
            routes += correct_action and actual_set == expected_set
        for name, counts in per_role.items():
            counts["tp"] += name in expected_set and name in actual_set
            counts["fp"] += name not in expected_set and name in actual_set
            counts["fn"] += name in expected_set and name not in actual_set
    for counts in per_role.values():
        denom = 2 * counts["tp"] + counts["fp"] + counts["fn"]
        counts["f1"] = 2 * counts["tp"] / denom if denom else None
    total = len(records)
    return {"total": total, "valid_count": valid, "valid_rate": valid / total, "action_accuracy": actions / total,
            "routing_exact_match": routes / total, "per_domain": per_role, "instruction_quality_evaluated": False,
            "scope": "Synthetic held-out smoke metrics; not evidence of real-world routing quality"}


def generate_report(records, predictor, split, metadata):
    selected = [row for row in records if row["split"] == split]
    predictions, errors, latencies = {}, {}, {}
    for row in selected:
        started = time.monotonic()
        try:
            output = predictor(request_from_dict(row["request"]))
            predictions[row["id"]] = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        except Exception as exc:
            predictions[row["id"]] = ""
            errors[row["id"]] = type(exc).__name__
        latencies[row["id"]] = round((time.monotonic() - started) * 1000, 2)
    return {"metadata": {**metadata, "split": split, "ids": [row["id"] for row in selected]}, "predictions": predictions,
            "errors": errors, "latency_ms": latencies, "metrics": score_predictions(selected, predictions, split)}


def compare_reports(baseline, candidate):
    for key in ("dataset_sha256", "prompt_sha256", "split", "ids"):
        if baseline["metadata"].get(key) != candidate["metadata"].get(key) or key not in baseline["metadata"]:
            raise ValueError(f"Reports are not comparable: {key}")
    isolated = (baseline["metadata"].get("backend") == "hf-base" and candidate["metadata"].get("backend") == "hf-adapter"
                and baseline["metadata"].get("revision") == candidate["metadata"].get("revision")
                and bool(baseline["metadata"].get("revision")))
    return {"same_hf_base_revision": isolated,
            "caution": "Inspect instruction quality and real-world data before accepting the adapter." if isolated else "Backends/revisions differ: changes cannot be attributed only to fine-tuning.",
            "deltas": {key: candidate["metrics"][key] - baseline["metrics"][key] for key in ("valid_rate", "action_accuracy", "routing_exact_match")}}


def read_json(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=unique_object)


def guarded_ollama(request):
    """Ollama decision with the orchestrator's deterministic guards applied — measures
    what the running system would actually emit, not just the raw model output."""
    decision = ollama_route(request)
    if decision["action"] == "route":
        options = ambiguous_item_options(request.query, request.available)
        if options:
            action = "handoff" if request.clarify_attempts >= 1 else "clarify"
            return {"action": action, "tasks": [],
                    "message": "Ambiguous request — which department?",
                    "options": options}
    elif decision["action"] == "clarify" and request.clarify_attempts >= 1:
        return {"action": "handoff", "tasks": [],
                "message": "I could not pin down the right department. Please contact the help desk directly.",
                "options": decision["options"]}
    return decision


def main():
    parser = argparse.ArgumentParser(description="Score predictions, generate model evaluations, or compare evaluation reports")
    parser.add_argument("predictions", nargs="?", type=Path)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--split", choices=("eval", "test", "heldout"), default="eval")
    parser.add_argument("--backend", choices=("ollama",))
    parser.add_argument("--guarded", action="store_true",
                        help="apply orchestrator decision-layer guards (ambiguity, clarify->handoff) before scoring")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("BASELINE", "CANDIDATE"))
    args = parser.parse_args()
    try:
        if args.compare:
            if args.backend or args.predictions:
                raise ValueError("Choose comparison, generation or scoring, not multiple modes")
            print(json.dumps(compare_reports(*(read_json(path) for path in args.compare)), indent=2))
            return
        records = load_records(args.data)
        if args.backend:
            if args.predictions or not args.output or args.output.exists() or not args.output.parent.is_dir():
                raise ValueError("Generation requires a new --output under an existing directory and no predictions input")
            metadata = {"backend": args.backend, "revision": None, "dataset_sha256": hashlib.sha256(args.data.read_bytes()).hexdigest(),
                        "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()}
            predictor = guarded_ollama if args.guarded else ollama_route
            metadata["guarded"] = bool(args.guarded)
            report = generate_report(records, predictor, args.split, metadata)
            with args.output.open("x", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
            print(json.dumps(report["metrics"], indent=2))
            print(f"Saved evaluation report: {args.output}")
        elif args.predictions:
            content = read_json(args.predictions)
            if "predictions" in content and "metadata" in content:
                split = content["metadata"]["split"]
                print(json.dumps(score_predictions(records, content["predictions"], split), indent=2))
            else:
                print(json.dumps(score_predictions(records, content, args.split), indent=2))
        else:
            raise ValueError("Provide predictions, --backend with --output, or --compare")
    except (ValueError, OSError, RuntimeError, ImportError) as exc:
        parser.exit(1, f"Evaluation failed: {exc}\n")


if __name__ == "__main__":
    main()
