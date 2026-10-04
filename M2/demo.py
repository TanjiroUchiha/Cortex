import argparse
import asyncio
import json
import re
import sys

from .fixtures import FIXTURES, load_fixture
from .service import M2Error, M2Merger, Settings


class OfflineDemoLLM:
    """Small test double for demonstrating the service without a running model."""

    async def complete(self, *, user_prompt: str, **_: object) -> str:
        content = json.loads(user_prompt.split("\n", 1)[1])
        if content["request"].startswith("What is the fee amount"):
            response = (
                "[FEES]\nThe semester fee is ₹12,000 and a ₹500 penalty applies.\n\n"
                "[IT]\nUse the student portal to pay the semester fee."
            )
            return json.dumps({"response": response}, ensure_ascii=False)

        sections: list[str] = []
        seen: list[str] = []
        for answer in content["domain_answers"]:
            text = answer["answer"]
            if not text:
                evidence = answer["evidence"]
                text = " ".join(
                    item if isinstance(item, str) else item.get("text", "")
                    for item in evidence
                )
            sentence = text.strip()
            normalized = re.sub(r"\W+", " ", sentence.lower()).strip()
            if sentence and not _is_duplicate(normalized, seen):
                sections.append(f"[{answer['domain'].upper()}]\n{sentence}")
                seen.append(normalized)
        return json.dumps({"response": "\n\n".join(sections)}, ensure_ascii=False)


def _is_duplicate(candidate: str, previous: list[str]) -> bool:
    words = set(candidate.split())
    facts = set(re.findall(r"\d+(?:[,.]\d+)*", candidate))
    for item in previous:
        previous_words = set(item.split())
        previous_facts = set(re.findall(r"\d+(?:[,.]\d+)*", item))
        smaller_size = min(len(words), len(previous_words))
        if (
            candidate == item
            or (
                smaller_size > 0
                and len(words & previous_words) / smaller_size >= 0.65
                and facts == previous_facts
            )
        ):
            return True
    return False


async def run(names: list[str], offline: bool = False) -> None:
    merger = (
        M2Merger(
            settings=Settings(endpoint="offline", model="offline-demo"),
            llm_client=OfflineDemoLLM(),
        )
        if offline
        else M2Merger()
    )
    for name in names:
        payload = load_fixture(name)
        print(f"\n=== {name} ===")
        print("INPUT DOMAIN ANSWERS")
        print(json.dumps(payload.model_dump(), indent=2, ensure_ascii=False))
        try:
            result = await merger.merge(payload)
            print("\nM2 DRAFT")
            print(result.model_dump_json(indent=2))
            print("\nVALIDATION: passed (or safe deterministic fallback)")
        except M2Error as error:
            print(f"\nM2 ERROR: {error}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Run M2 using mock domain-skill fixtures.")
    parser.add_argument(
        "fixtures",
        nargs="*",
        choices=sorted(FIXTURES),
        help="Fixture names (default: run all except no_evidence).",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use a local demo-only fake LLM; does not contact a model endpoint.",
    )
    args = parser.parse_args()
    selected = args.fixtures or [
        name for name in FIXTURES if name not in {"no_evidence"}
    ]
    asyncio.run(run(selected, offline=args.offline))


if __name__ == "__main__":
    main()
