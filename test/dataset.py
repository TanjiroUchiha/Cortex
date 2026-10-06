import argparse
import json
from collections import Counter
from pathlib import Path

from models.m1 import parse_decision, request_from_dict, require_keys, require_text, unique_object

DEFAULT_DATA = Path(__file__).resolve().parent.parent / "dataset" / "starter.json"


def validate_records(records):
    if not isinstance(records, list) or not records:
        raise ValueError("Dataset must be a nonempty list")
    ids, prompts, groups, splits = set(), set(), {}, set()
    for row in records:
        require_keys(row, ("id", "group", "split", "source", "reviewed", "request", "decision"), "record")
        for field in ("id", "group", "source"):
            require_text(row[field], field, 100)
        if row["split"] not in ("train", "eval", "test", "heldout") or type(row["reviewed"]) is not bool:
            raise ValueError("Invalid split or reviewed flag")
        request = request_from_dict(row["request"])
        parse_decision(json.dumps(row["decision"]), request)
        normalized = " ".join(request.query.casefold().split())
        if row["id"] in ids or normalized in prompts:
            raise ValueError(f"Duplicate record or normalized query: {row['id']}")
        if row["group"] in groups and groups[row["group"]] != row["split"]:
            raise ValueError(f"Scenario group leaks across splits: {row['group']}")
        ids.add(row["id"])
        prompts.add(normalized)
        groups[row["group"]] = row["split"]
        splits.add(row["split"])
    if not {"train", "eval"}.issubset(splits):
        raise ValueError("Both train and eval splits are required")
    return records


def load_records(path=DEFAULT_DATA):
    with Path(path).open(encoding="utf-8") as handle:
        return validate_records(json.load(handle, object_pairs_hook=unique_object))


def main():
    parser = argparse.ArgumentParser(description="Validate routing data without loading any model")
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_DATA)
    args = parser.parse_args()
    try:
        records = load_records(args.path)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Dataset validation failed: {exc}\n")
    print(json.dumps({"records": len(records), "splits": dict(Counter(r["split"] for r in records)), "unreviewed": sum(not r["reviewed"] for r in records), "status": "schema_valid_not_quality_validated"}, indent=2))


if __name__ == "__main__":
    main()
