import unittest
from models.v1_checks import verify

EV = {"domain": "fees",
      "answer": "The last date for semester fee payment is October 15, 2026.",
      "citations": [{"doc_id": "fees-calendar", "title": "Fee calendar"}],
      "evidence": [{"doc_id": "fees-calendar",
                    "chunk": "The last date for semester fee payment is October 15, 2026."}]}
CIT = [{"doc_id": "fees-calendar", "title": "Fee calendar"}]


def run(resp, cits=CIT, answers=(EV,), failures=(), q="What is the semester fee deadline?"):
    return verify({"request": {"query": q}, "response": resp, "citations": cits,
                   "domain_answers": list(answers), "failures": list(failures)})


class T(unittest.TestCase):
    def test_good(self):
        self.assertEqual(run("The last date for semester fee payment is October 15, 2026.")["status"], "passed")

    def test_reworded_date(self):
        self.assertEqual(run("Semester fee payment closes 15 October 2026.")["status"], "passed")

    def test_invented_number(self):
        r = run("The last date for semester fee payment is October 15, 2026. A 500 penalty applies.")
        self.assertEqual(r["status"], "failed"); self.assertIn("number_mismatch", r["flags"])

    def test_wrong_month(self):
        self.assertIn("number_mismatch", run("Semester fee payment is due November 15, 2026.")["flags"])

    def test_bad_citation(self):
        r = run("The last date for semester fee payment is October 15, 2026.", cits=[{"doc_id": "hr-x"}])
        self.assertEqual(r["status"], "failed"); self.assertIn("ungrounded_citation", r["flags"])

    def test_invented_email(self):
        self.assertIn("unsupported_contact", run("Fee payment deadline is October 15, 2026. Mail fees@fake.edu.")["flags"])

    def test_empty(self):
        self.assertEqual(run("")["status"], "failed")

    def test_no_evidence(self):
        self.assertEqual(run("Pets are allowed.", cits=[], answers=[])["status"], "uncertain")

    def test_unsupported_sentence(self):
        r = run("The last date for semester fee payment is October 15, 2026. Hostel rooms include free laundry service.")
        self.assertEqual(r["status"], "uncertain")

REAL = ("[FEES] For urgent bank deadlines or a letter that must name a specific officer, write to the "
        "Student Accounts Office - fees@campus.edu (ext 4500) - with the bank's deadline. The fees portal "
        "shows your exact dues and the deadline for each head.")
REAL_EV = {"domain": "fees", "answer": REAL, "citations": [{"doc_id": "fees-payment-deadlines", "title": "x"}],
           "evidence": [{"doc_id": "fees-payment-deadlines", "chunk": REAL}]}


class Q(unittest.TestCase):
    def test_real_draft_ext_number_is_not_a_date(self):
        r = verify({"request": {"query": "What is the fee deadline?"}, "response": REAL,
                    "citations": [{"doc_id": "fees-payment-deadlines"}], "domain_answers": [REAL_EV]})
        self.assertIn("incomplete_answer", r["flags"]); self.assertEqual(r["status"], "uncertain")

    def test_may_verb_is_not_month(self):
        ev = dict(EV, answer="Students may pay online.", evidence=[{"doc_id": "fees-calendar", "chunk": "Students pay online."}])
        r = run("Students may pay online.", answers=[ev], q="how to pay")
        self.assertNotIn("number_mismatch", r["flags"])

    def test_relative_period_ok_for_when_question(self):
        ev = dict(EV, answer="Refunds are processed within ten working days.",
                  evidence=[{"doc_id": "fees-calendar", "chunk": "Refunds are processed within ten working days."}])
        r = run("Refunds are processed within ten working days.", answers=[ev], q="When will my refund be processed?")
        self.assertNotIn("incomplete_answer", r["flags"])

    def test_relative_period_is_not_a_deadline(self):
        txt = ("Semester fees must be paid by the date on the academic calendar. "
               "The fees office confirms refunds within three working days.")
        ev = dict(EV, answer=txt, evidence=[{"doc_id": "fees-calendar", "chunk": txt}])
        r = run(txt, answers=[ev], q="What is the last date to pay semester fees?")
        self.assertIn("incomplete_answer", r["flags"])

    def test_when_without_date(self):
        ans = dict(EV, answer="Fees are paid through the student portal.",
                   evidence=[{"doc_id": "fees-calendar", "chunk": "Fees are paid through the student portal."}])
        r = run("Fees are paid through the student portal.", answers=[ans])
        self.assertIn("incomplete_answer", r["flags"]); self.assertEqual(r["status"], "uncertain")

    def test_abstain(self):
        ans = dict(EV, answer="", evidence=[])
        r = run("I could not find that in the documents.", cits=[], answers=[ans])
        self.assertIn("no_answer", r["flags"])

    def test_each_requested_part_must_be_in_the_response_not_just_context(self):
        it = {"domain": "it", "answer": "Reset your password at the self-service portal.",
              "citations": [{"doc_id": "it-pw"}],
              "evidence": [{"doc_id": "it-pw", "chunk": "Reset your password at the self-service portal."}]}
        r = run("The last date for semester fee payment is October 15, 2026.",
                cits=CIT + [{"doc_id": "it-pw"}], answers=[EV, it],
                q="Fee deadline and how do I reset my password?")
        self.assertIn("part_unanswered", r["flags"])
        self.assertNotIn("missing_domain", r["flags"])

    def test_direct_contact_answer_is_not_rejected_for_omitting_unrelated_domain_text(self):
        contact = "HR Service Centre email: hr@campus.edu."
        unrelated = "Changes to personal details are submitted through the HR self-service portal."
        answer = {"domain": "hr", "answer": unrelated + " " + contact,
                  "citations": [{"doc_id": "hr-service"}],
                  "evidence": [{"doc_id": "hr-service", "chunk": unrelated + " " + contact}]}
        result = run("Contact the HR Service Centre at hr@campus.edu.",
                     cits=answer["citations"], answers=[answer], q="How do I contact HR?")
        self.assertEqual(result["status"], "passed")
        self.assertNotIn("missing_domain", result["flags"])

    def test_string_request_ok(self):
        r = verify({"request": "fee deadline?", "response": "Payment closes October 15, 2026.",
                    "citations": CIT, "domain_answers": [EV]})
        self.assertEqual(r["status"], "passed")


class Cov(unittest.TestCase):
    def _ans(self, text):
        return {"domain": "facilities", "answer": text, "citations": [{"doc_id": "d1", "title": "t"}],
                "evidence": [{"doc_id": "d1", "chunk": text}]}

    def _run(self, q, text):
        return verify({"request": {"query": q}, "response": text, "citations": [{"doc_id": "d1"}],
                       "domain_answers": [self._ans(text)]})

    def test_pet_question_off_topic_answer(self):
        r = self._run("Can I bring a pet to my hostel room?", "Hostel rooms must be booked through the facilities portal.")
        self.assertIn("part_unanswered", r["flags"]); self.assertEqual(r["status"], "uncertain")

    def test_second_half_never_answered(self):
        r = self._run("Wi-Fi not working and I need a fee receipt", "Fee receipts are available on the fees portal.")
        self.assertIn("part_unanswered", r["flags"])

    def test_clean_howto_passes(self):
        r = self._run("How do I reset my password?", "Reset your password through the self-service portal.")
        self.assertEqual(r["status"], "passed")

    def test_wifi_half_not_hidden_by_working_days(self):
        txt = "Receipts are corrected on the fees portal and reissued within three working days."
        r = self._run("Wi-Fi not working and I need a fee receipt", txt)
        self.assertIn("part_unanswered", r["flags"])

    def test_hours_without_times(self):
        r = self._run("What are the gym opening hours?", "The gym runs morning and evening slots; register on the portal.")
        self.assertIn("incomplete_answer", r["flags"])

    def test_email_question_needs_an_email(self):
        r = self._run("What is the IT helpdesk email?", "Contact the IT helpdesk for account problems.")
        self.assertIn("incomplete_answer", r["flags"])

    def test_email_question_with_email_passes(self):
        r = self._run("What is the IT helpdesk email?", "Email the IT helpdesk at it-help@campus.edu.")
        self.assertNotIn("incomplete_answer", r["flags"])

    def test_pet_real_draft(self):
        txt = ("Hostel room allocation, room changes and in-room repairs are handled by the facilities hostel desk "
               "in the admin block. Raise a repair request on the facilities portal with your hostel block.")
        self.assertIn("part_unanswered", self._run("Can I bring a pet to my hostel room?", txt)["flags"])

    def test_plural_and_singular_match(self):
        r = self._run("Who do I contact about a fee refund?", "Refunds are filed on the fees portal. Contact the Student Accounts Office, ext 4500.")
        self.assertNotIn("part_unanswered", r["flags"])

    def test_gym_hours_passes(self):
        r = self._run("What are the gym opening hours?", "The gym is open from 6am to 10pm daily.")
        self.assertNotIn("part_unanswered", r["flags"])


class GroundingRegressionTests(unittest.TestCase):
    def test_generated_answer_cannot_ground_its_own_number(self):
        answer = dict(EV, answer=EV["answer"] + " A 99999 penalty applies.")
        self.assertIn("number_mismatch", run(answer["answer"], answers=[answer])["flags"])

    def test_answer_without_evidence_is_not_verified(self):
        answer = dict(EV, evidence=[])
        self.assertNotEqual(run(answer["answer"], answers=[answer])["status"], "passed")

    def test_citation_must_identify_an_evidence_chunk(self):
        answer = dict(EV, citations=[{"doc_id": "invented"}])
        self.assertIn("ungrounded_citation", run(answer["answer"], cits=answer["citations"],
                                                 answers=[answer])["flags"])

    def test_contact_must_match_whole_address(self):
        text = "Email admin-fees@campus.edu for fee questions."
        answer = dict(EV, answer=text, evidence=[{"doc_id": "fees-calendar", "chunk": text}])
        self.assertIn("unsupported_contact", run("Email fees@campus.edu for fee questions.",
                                                 answers=[answer], q="Fee contact email")["flags"])

    def test_similar_prefix_is_not_query_coverage(self):
        from models.v1_checks import uncovered_part
        self.assertIsNotNone(uncovered_part("scholarship", "The school opens today.", []))
        self.assertIsNotNone(uncovered_part("unpublished", "The published timetable is available.", []))

    def test_full_word_typo_is_covered(self):
        from models.v1_checks import uncovered_part
        self.assertIsNone(uncovered_part("scolarship", "Scholarships are applied online.", []))
        self.assertIsNone(uncovered_part("pasword", "Reset your password online.", []))

    def test_when_is_accepts_recurring_period(self):
        text = "Salary is paid on the last working day of each month."
        answer = dict(EV, answer=text, evidence=[{"doc_id": "fees-calendar", "chunk": text}])
        self.assertNotIn("incomplete_answer", run(text, answers=[answer], q="When is salary paid?")["flags"])

    def test_contact_desk_is_a_contact(self):
        text = "Contact the scholarship office for scholarship applications."
        answer = dict(EV, answer=text, evidence=[{"doc_id": "fees-calendar", "chunk": text}])
        self.assertNotIn("incomplete_answer", run(text, answers=[answer], q="Who do I contact about scholarships?")["flags"])

    def test_checker_exception_never_passes(self):
        import asyncio
        from unittest.mock import patch
        from backend.services import CheckVerifier
        payload = {"request": {"query": "fee deadline"}, "response": EV["answer"],
                   "domain_answers": [EV], "citations": CIT}
        with patch("backend.services.v1_verify", side_effect=ValueError("bad payload")):
            result = asyncio.run(CheckVerifier()(payload, "test-checker"))
        self.assertNotEqual(result["status"], "passed")


if __name__ == "__main__":
    unittest.main()