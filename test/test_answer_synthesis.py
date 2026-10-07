import asyncio
import json
import unittest

from backend.orchestrator import merge_answers
from backend.services import MERGE_SYSTEM, OllamaMerger
from models.m2.safety import OutputValidationError, deterministic_merge, validate_response
from models.m2.schemas import DomainAnswer, M2Input
from models.m2.service import M2Merger, Settings


def answer(domain, doc_id, text):
    return {
        "domain": domain,
        "answer": text,
        "citations": [{"doc_id": doc_id, "title": f"{domain.title()} source"}],
        "evidence": [{"doc_id": doc_id, "chunk": text}],
    }


class ModelHandler:
    def __init__(self, response):
        self.response = response
        self.payload = None

    async def __call__(self, payload, request_id):
        self.payload = payload
        return {"done": True, "done_reason": "stop",
                "message": {"content": json.dumps({"response": self.response})}}


class AnswerSynthesisTests(unittest.TestCase):
    def run_merger(self, query, answers, response, failures=()):
        handler = ModelHandler(response)
        merger = OllamaMerger(handler=handler)
        result = asyncio.run(merger({
            "request": {"query": query},
            "domain_answers": answers,
            "failures": list(failures),
        }, "answer-test"))
        return result, handler

    def test_simple_question_returns_short_natural_paragraph(self):
        source = "The semester fee payment deadline is October 15, 2026."
        result, handler = self.run_merger(
            "What is the semester fee deadline?",
            [answer("fees", "fee-calendar", source)],
            "Semester fees are due by October 15, 2026.",
        )
        self.assertEqual(result["response"], "Semester fees are due by October 15, 2026.")
        self.assertEqual(handler.payload["format"]["required"], ["response"])
        self.assertIn("Do not expose internal domain names", MERGE_SYSTEM)

    def test_multi_document_question_combines_points_without_domain_labels(self):
        it = answer("it", "wifi-guide", "If Wi-Fi is down across campus, check the IT status page.")
        hr = answer("hr", "payslip-guide", "Download your payslip from the employee portal.")
        result, _ = self.run_merger(
            "What should I do about campus Wi-Fi and my payslip?",
            [it, hr],
            "- Check the IT status page if Wi-Fi is down across campus.\n"
            "- Download your payslip from the employee portal.",
        )
        self.assertIn("Check the IT status page", result["response"])
        self.assertIn("Download your payslip", result["response"])
        self.assertNotRegex(result["response"], r"(?i)\[(?:IT|HR|FEES|GENERAL)\]")
        self.assertEqual(len(result["citations"]), 2)

    def test_process_question_keeps_source_order_in_numbered_steps(self):
        source = ("Open the account settings page. Select Security. "
                  "Choose Change password and follow the on-screen prompts.")
        result, _ = self.run_merger(
            "How do I change my password? Give me the steps.",
            [answer("it", "password-guide", source)],
            "1. Open the account settings page.\n"
            "2. Select Security.\n"
            "3. Choose Change password and follow the on-screen prompts.",
        )
        self.assertIn("1. Open the account settings page.", result["response"])
        self.assertIn("3. Choose Change password", result["response"])

    def test_comparison_question_can_return_a_table(self):
        library = answer("general", "library-hours",
                         "The library is open from 8 a.m. to 8 p.m.")
        lab = answer("facilities", "lab-hours",
                     "The computer lab is open from 9 a.m. to 6 p.m.")
        table = ("| Location | Opening hours |\n"
                 "| --- | --- |\n"
                 "| Library | 8 a.m. to 8 p.m. |\n"
                 "| Computer lab | 9 a.m. to 6 p.m. |")
        result, _ = self.run_merger(
            "Compare the library and computer lab opening hours.",
            [library, lab],
            table,
        )
        self.assertIn("| Library |", result["response"])
        self.assertIn("| Computer lab |", result["response"])

    def test_missing_information_is_disclosed_and_unsupported_claim_rejected(self):
        fees = answer("fees", "fee-calendar",
                      "The semester fee payment deadline is October 15, 2026.")
        result, _ = self.run_merger(
            "What is the fee deadline, and when does HR issue payslips?",
            [fees],
            "The fee deadline is October 15, 2026. HR issues payslips within 24 hours.",
            failures=[{"service": "hr", "code": "no_evidence"}],
        )
        self.assertIn("October 15, 2026", result["response"])
        self.assertIn("couldn't find enough information", result["response"])
        self.assertNotIn("24 hours", result["response"])
        self.assertNotRegex(result["response"], r"(?i)\[(?:IT|HR|FEES|GENERAL)\]")

    def test_deterministic_fallback_deduplicates_and_hides_labels(self):
        text = "The portal is open daily. The portal is open daily."
        result = merge_answers([
            answer("fees", "portal-a", f"[FEES] {text}"),
            answer("general", "portal-b", "The portal is open daily."),
        ])
        self.assertEqual(result["response"], "The portal is open daily.")
        self.assertNotIn("[FEES]", result["response"])
        self.assertEqual(len(result["citations"]), 2)


class StandaloneM2SafetyTests(unittest.TestCase):
    def test_standalone_m2_falls_back_if_model_violates_response_contract(self):
        class InvalidModel:
            async def complete(self, **kwargs):
                return '{"request":"echoed input instead of an answer"}'

        source = "Download your payslip from the employee portal."
        payload = {
            "request_id": "bad-model-output",
            "request": "How do I get my payslip?",
            "domain_answers": [{
                "domain": "hr", "answer": source,
                "citations": [{"doc_id": "payslip", "title": "Payslip guide"}],
                "evidence": [{"doc_id": "payslip", "text": source}],
            }],
        }
        merger = M2Merger(
            Settings(endpoint="http://127.0.0.1:11434/api/chat", model="qwen3:4b"),
            llm_client=InvalidModel(),
        )
        result = asyncio.run(merger.merge(M2Input.model_validate(payload)))
        self.assertEqual(result.response, source)
        self.assertNotIn("[HR]", result.response)

    def test_standalone_m2_accepts_numbered_steps_without_inventing_facts(self):
        source = ("Open the account settings page. Select Security. "
                  "Choose Change password and follow the on-screen prompts.")
        answer_data = DomainAnswer.model_validate({
            "domain": "it", "answer": source,
            "evidence": [{"doc_id": "password-guide", "text": source}],
        })
        validate_response(
            "1. Open the account settings page.\n"
            "2. Select Security.\n"
            "3. Choose Change password and follow the on-screen prompts.",
            [answer_data],
        )
        with self.assertRaises(OutputValidationError):
            validate_response(
                "1. Open the account settings page.\n"
                "2. Select Security. The reset is guaranteed within 24 hours.",
                [answer_data],
            )

    def test_standalone_m2_fallback_hides_routing_labels(self):
        answer_data = DomainAnswer.model_validate({
            "domain": "fees", "answer": "[FEES] Payment is due on October 15.",
        })
        result = deterministic_merge([answer_data])
        self.assertEqual(result, "Payment is due on October 15.")
        self.assertNotIn("[FEES]", result)


if __name__ == "__main__":
    unittest.main()
