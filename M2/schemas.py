from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DOMAINS = {"it", "hr", "fees", "facilities", "general"}

CitationValue = str | dict[str, Any]
EvidenceValue = str | dict[str, Any]

# Keys a model might use for the merged text; the first non-empty string wins.
RESPONSE_KEYS = ("response", "answer", "reply", "merged_response", "merged", "text", "content", "result")


def _clean_domain(value: Any) -> str:
    domain = str(value).strip().lower()
    if domain not in DOMAINS:
        raise ValueError(f"Unsupported domain: {value}")
    return domain


class DomainAnswer(BaseModel):
    # ignore unknown keys (e.g. M1's "location") instead of rejecting the request
    model_config = ConfigDict(extra="ignore")

    domain: str
    answer: str = Field(default="", max_length=20_000)
    citations: list[CitationValue] = Field(default_factory=list, max_length=100)
    evidence: list[EvidenceValue] = Field(default_factory=list, max_length=100)

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        return _clean_domain(value)

    @field_validator("evidence", mode="before")
    @classmethod
    def normalize_evidence(cls, value: Any) -> Any:
        # M1 sends {"doc_id", "chunk"}; the service reads "text"
        if not isinstance(value, list):
            return value
        fixed = []
        for item in value:
            if isinstance(item, dict) and "chunk" in item and "text" not in item:
                item = {**item, "text": item["chunk"]}
            fixed.append(item)
        return fixed


class DomainFailure(BaseModel):
    model_config = ConfigDict(extra="ignore")

    domain: str
    error: str = "failed"

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        return _clean_domain(value)

    @model_validator(mode="before")
    @classmethod
    def accept_code_as_error(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("error") and data.get("code"):
            return {**data, "error": str(data["code"])}
        return data


class M2Input(BaseModel):
    model_config = ConfigDict(extra="ignore")

    request_id: str = Field(min_length=1, max_length=200)
    request: str = Field(min_length=1, max_length=10_000)
    domain_answers: list[DomainAnswer] = Field(default_factory=list)
    failures: list[DomainFailure] = Field(default_factory=list)

    @field_validator("request", mode="before")
    @classmethod
    def request_as_text(cls, value: Any) -> Any:
        # accept M1's request object as well as a plain string
        if isinstance(value, dict):
            return str(value.get("query", ""))
        return value

    @model_validator(mode="after")
    def validate_domain_uniqueness(self) -> "M2Input":
        answer_domains = [item.domain for item in self.domain_answers]
        if len(answer_domains) != len(set(answer_domains)):
            raise ValueError("Each domain may appear at most once in domain_answers")
        failed_domains = {item.domain for item in self.failures}
        if failed_domains.intersection(answer_domains):
            raise ValueError("A domain cannot be both successful and failed")
        return self


class M2Response(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response: str = Field(min_length=1)
    citations: list[CitationValue] = Field(default_factory=list)


class ModelResponse(BaseModel):
    # the model may add extra keys or pick another key name; take the text, ignore the rest
    model_config = ConfigDict(extra="ignore")

    response: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def find_response_text(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"response": data}
        if isinstance(data, dict):
            for key in RESPONSE_KEYS:
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return {"response": value}
        return data