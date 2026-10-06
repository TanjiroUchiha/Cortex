import time
import httpx

URL = "http://127.0.0.1:9001/merge"

def post(label, body):
    t = time.time()
    try:
        r = httpx.post(URL, json=body, timeout=130)
        print(label, "->", r.status_code, f"{time.time() - t:.1f}s")
        print(r.text[:600], "\n")
    except Exception as e:
        print(label, "-> FAILED", repr(e), f"{time.time() - t:.1f}s\n")

it = {"domain": "it", "answer": "Reset your password at the IT help portal.",
      "citations": [{"doc_id": "it-1", "title": "Password help"}],
      "evidence": [{"doc_id": "it-1", "text": "Reset passwords at the IT help portal."}]}
fees = {"domain": "fees", "answer": "Hostel fees are due on the 10th of each month.",
        "citations": [{"doc_id": "fee-1", "title": "Fee schedule"}],
        "evidence": [{"doc_id": "fee-1", "text": "Hostel fees are due on the 10th."}]}

post("ONE domain (no model call)", {"request_id": "p1", "request": "reset my password",
                                     "domain_answers": [it], "failures": []})
post("TWO domains (model call)", {"request_id": "p2", "request": "password and hostel fee deadline",
                                   "domain_answers": [it, fees], "failures": []})