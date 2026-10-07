"""V1 deterministic verifier. No model calls: code decides hard failures.

Input  : {request_id, request, domain_answers, failures, response, citations}
Output : {"status": "passed|failed|uncertain", "flags": [...], "explanation": str}
"""
from __future__ import annotations
import difflib
import re

HARD = {"empty_response", "ungrounded_citation", "number_mismatch", "unsupported_contact"}
SOFT = {"unsupported_sentence", "no_evidence_to_check", "coverage_gap",
        "incomplete_answer", "no_answer", "part_unanswered",
        "off_topic"}

MONTHS = ("january february march april may june july august september "
          "october november december jan feb mar apr jun jul aug sep sept oct nov dec").split()
STOP = set("the a an and or of to in on for is are was be by with as at it this that "
           "from you your can will may must should not have has any all if then".split())

NUM_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
URL_RE = re.compile(r"https?://\S+|www\.\S+")
WORD_RE = re.compile(r"[a-z][a-z'-]{3,}")


def _text(item) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for k in ("text", "chunk", "answer"):
            if isinstance(item.get(k), str):
                return item[k]
    return ""


def _nums(s: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in NUM_RE.findall(s)}


MONTH_NAMES = ("january|february|march|april|may|june|july|august|september|october|"
               "november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec")
MONTH_DATE_RE = re.compile(
    rf"\b(?:({MONTH_NAMES})\.?\s+(?:\d{{1,2}}\b|\d{{4}}\b)"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?({MONTH_NAMES})\b)", re.I)
DATE_ABS_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b|\b\d{1,2}(?:st|nd|rd|th)\b", re.I)
DATE_REL_RE = re.compile(r"\b(?:within|after|before|by)\s+(?:\w+\s+){0,2}(?:days?|weeks?|hours?)\b", re.I)
PERIOD_RE = re.compile(r"\b(?:each|every|per)\s+(?:day|week|month|year|semester|working day)\b"
                       r"|\b(?:last|first|end of|beginning of)\s+\w*\s*(?:day|week|month)\b", re.I)
TIME_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|\b\d{1,2}:\d{2}\b", re.I)
PHONE_RE = re.compile(r"ext\.?\s*\d+|\+?\d[\d\s-]{6,}\d", re.I)


def _months(s: str) -> set[str]:
    return {(m.group(1) or m.group(2))[:3].lower() for m in MONTH_DATE_RE.finditer(s)}


def _sentences(s: str) -> list[str]:
    lines = s.splitlines()
    normalized = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if "|" in stripped:
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if cells and all(re.fullmatch(r"[:\-\s]+", cell or "-") for cell in cells):
                continue
            if index + 1 < len(lines) and re.fullmatch(
                    r"\s*\|?[\s:|-]+\|?\s*", lines[index + 1]):
                continue
            stripped = " ".join(cell for cell in cells if cell)
        normalized.append(stripped)
    text = "\n".join(normalized)
    return [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text) if len(p.strip()) > 12]


def _query(payload) -> str:
    r = payload.get("request")
    return (r.get("query", "") if isinstance(r, dict) else str(r or "")).strip()


ABSTAIN_RE = re.compile(r"could not|couldn't|can't find|cannot find|no information|not covered|unable to", re.I)
# (question pattern, what a satisfying answer must contain, flag note)
INTENTS = [   # (question pattern, what a satisfying answer must contain) - checked in order
    (re.compile(r"\b(last date|deadline|due date|by what date|exam date)\b", re.I), "date"),
    (re.compile(r"\bwhen\b", re.I), "date_or_period"),
    (re.compile(r"\b(how much|how many|cost|price|fee amount|charges?)\b", re.I), "number"),
    (re.compile(r"\b(hours|timings?|opening|schedule|what time)\b", re.I), "time"),
    (re.compile(r"\be-?mail\b", re.I), "email"),
    (re.compile(r"\b(phone|call|extension)\b", re.I), "phone"),
    (re.compile(r"\b(contact|reach)\b", re.I), "contact"),
]


# words that carry no topic (question words, fillers, and intent words the date/number/contact rules already cover)
QSTOP = set("""what when where which who whom how why does did can could would should will shall need want
please tell about there have has get give the and for with from into this that you your are was were not any
all some also plus been being its just like using use deadline date due last much many cost price contact
email phone reach call step steps process procedure compare comparison difference between versus""".split())
CLAUSE_SPLIT = re.compile(r"\band\b|\balso\b|\bplus\b|[,;?]", re.I)


def _flat(text: str) -> str:
    flat = re.sub(r"(?<=[a-z])-(?=[a-z])", "", text.lower())   # wi-fi -> wifi
    return re.sub(r"\bhelp\s+desk\b", "helpdesk", flat)


def _stem(w: str) -> str:
    return (w[:-1] if w.endswith("s") and len(w) > 3 else w)[:4]     # fees->fee, hours->hour


def _stems(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z]{3,}", _flat(text))}


def _covered(word: str, have: set[str]) -> bool:
    """Stem match or near-match: a typo'd query term ('scolarship') still counts
    as covered by its correct spelling in the evidence ('scholarship')."""
    singular = lambda w: w[:-1] if len(w) > 3 and w.endswith("s") else w
    if word in have or any(singular(word) == singular(w) for w in have):
        return True
    if singular(word) == "fee" and any(singular(w) in {"fee", "payment", "tuition"} for w in have):
        return True
    if len(word) < 5:
        return False
    for candidate in difflib.get_close_matches(word, sorted(have), n=2, cutoff=0.87):
        if any(word.startswith(p) != candidate.startswith(p)
               and (word.startswith(p) or candidate.startswith(p))
               for p in ("un", "non", "dis", "mis", "im", "il", "ir")):
            continue
        return True
    return False


def uncovered_part(query: str, response: str, answers: list[dict]) -> str | None:
    """Split the question into parts; retrieved context alone does not count as an answer."""
    have = set(re.findall(r"[a-z]{3,}", _flat(response)))
    for clause in CLAUSE_SPLIT.split(query):
        terms = [w for w in re.findall(r"[a-z]{3,}", _flat(clause)) if w not in QSTOP]
        if TIME_RE.search(response) and re.search(r"\b(when|time|hours|timings|opening|closing)\b", clause, re.I):
            terms = [w for w in terms if w not in {"time", "hours", "timings", "opening", "closing", "open", "close"}]
        if terms and sum(_covered(w, have) for w in terms) / len(terms) <= 0.5:
            return clause.strip()
    return None


def satisfaction(query: str, response: str, answers: list[dict]) -> tuple[list[str], list[str]]:
    """Does the response address the question? Soft flags only: relevance is fuzzy,
    so code never hard-fails on it (a false fail blocks a good answer)."""
    flags, notes = [], []
    if not query:
        return flags, notes
    stripped = EMAIL_RE.sub("", re.sub(r"ext\.?\s*\d+", "", response, flags=re.I))
    has_num = bool(NUM_RE.search(stripped))
    abs_date = bool(_months(response) or DATE_ABS_RE.search(response))
    has_phone = bool(PHONE_RE.search(response))
    has_email = bool(EMAIL_RE.search(response))
    ok = {"date": abs_date,                                   # a deadline needs a real date, not "within 3 days"
          "date_or_period": abs_date or bool(DATE_REL_RE.search(response))
                            or bool(PERIOD_RE.search(response))
                            or bool(TIME_RE.search(response)),  # "8am-10pm" answers "when"
          "number": has_num,
          "time": bool(TIME_RE.search(response)),
          "email": has_email,
          "phone": has_phone,
          "contact": has_email or has_phone or bool(URL_RE.search(response))
                     or bool(re.search(r"\b(office|desk|helpdesk|counter|team)\b", response, re.I))}
    for rx, need in INTENTS:
        if rx.search(query) and not ok[need]:
            flags.append("incomplete_answer")
            notes.append(f"question asks for a {need} but the response has none")
            break
    if ABSTAIN_RE.search(response) and not has_num:
        flags.append("no_answer")
        notes.append("response says it could not answer")
    part = uncovered_part(query, response, answers)
    if part:
        flags.append("part_unanswered")
        notes.append(f"nothing in the response covers: {part!r}")
    # topical relevance: the response itself must carry the question's subject
    # vocabulary. A grounded paragraph on an adjacent topic ("admin password?"
    # → a lab-credentials policy) passes citation checks while answering a
    # different question entirely.
    q_terms = [w for w in re.findall(r"[a-z]{3,}", _flat(query)) if w not in QSTOP]
    r_terms = set(re.findall(r"[a-z]{3,}", _flat(response)))
    r_stems = _stems(response)
    if len(q_terms) >= 3:   # a single term is too fragile — "fee"↔"payment" is real coverage
        covered = sum(_covered(w, r_terms) or _stem(w) in r_stems for w in q_terms)
        if covered / len(q_terms) < 0.30:
            flags.append("off_topic")
            notes.append("response does not address the question's subject")
    return flags, notes


def verify(payload: dict) -> dict:
    response = (payload.get("response") or "").strip()
    answers = payload.get("domain_answers") or []
    flags: list[str] = []
    notes: list[str] = []

    if not response:
        return {"status": "failed", "flags": ["empty_response"],
                "explanation": "Response is empty."}

    # Evidence pool: evidence text + the domain answers themselves (extractive).
    docs: set[str] = set()
    pool: list[str] = []
    for a in answers:
        for e in a.get("evidence") or []:
            if _text(e):
                pool.append(_text(e))
                if e.get("doc_id"):
                    docs.add(e["doc_id"])
    pool_text = "\n".join(p for p in pool if p)

    # 1. Citation trace
    for c in payload.get("citations") or []:
        if c.get("doc_id") not in docs:
            flags.append("ungrounded_citation")
            notes.append(f"citation {c.get('doc_id')!r} not in any domain answer")
            break

    if not pool_text:
        flags.append("no_evidence_to_check")
        notes.append("no domain evidence supplied; cannot verify facts")
    else:
        low = pool_text.lower()
        # 2. Fact tokens: numbers, emails, URLs, months
        numbered_steps_removed = re.sub(r"(?m)^\s*\d+[.)]\s+", "", response)
        missing = _nums(numbered_steps_removed) - _nums(pool_text)
        missing_m = _months(response) - _months(pool_text)
        if missing or missing_m:
            flags.append("number_mismatch")
            notes.append(f"not in evidence: {sorted(missing | missing_m)}")
        for rx in (EMAIL_RE, URL_RE):
            supported = {t.lower().rstrip('.,)') for t in rx.findall(pool_text)}
            bad = [t for t in rx.findall(response) if t.lower().rstrip('.,)') not in supported]
            if bad:
                flags.append("unsupported_contact")
                notes.append(f"not in evidence: {bad}")
                break
        # 3. Sentence support (content-word overlap with evidence)
        ev_words = set(WORD_RE.findall(low)) - STOP
        for s in _sentences(response):
            w = set(WORD_RE.findall(s.lower())) - STOP
            if w and len(w & ev_words) / len(w) < 0.4:
                flags.append("unsupported_sentence")
                notes.append(f"weak support: {s[:80]!r}")
                break

    # 4. Query satisfaction (does it answer what was asked?)
    sf, sn = satisfaction(_query(payload), response, answers)
    flags += sf
    notes += sn

    # 5. Coverage: failures reported but answer cites nothing / no answers at all
    if payload.get("failures") and not answers and payload.get("citations"):
        flags.append("coverage_gap")

    flags = list(dict.fromkeys(flags))
    if any(f in HARD for f in flags):
        status = "failed"
    elif flags:
        status = "uncertain"
    else:
        status = "passed"
    return {"status": status, "flags": flags,
            "explanation": "; ".join(notes) or "All deterministic checks passed."}