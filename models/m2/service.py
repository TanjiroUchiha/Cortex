"""M2 configuration, model adapter, errors, and synthesis service."""

import asyncio
from dataclasses import dataclass
import json
import logging
import os
import time
from typing import Any, Protocol
from xml.parsers.expat import model

import httpx
from pydantic import ValidationError

from .safety import (
    SYSTEM_PROMPT,
    OutputValidationError,
    deterministic_merge,
    make_user_prompt,
    validate_response,
)
from .schemas import M2Input, M2Response, ModelResponse

logger = logging.getLogger("cortex_m2")
MAX_RETRIES = 1
M2_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"response": {"type": "string"}},
    "required": ["response"],
}


@dataclass(frozen=True)
class Settings:
    endpoint: str | None
    model: str | None
    api_key: str | None = None
    timeout_seconds: float = 20.0
    max_evidence_chunks: int = 5
    max_evidence_chars: int = 1600

    @classmethod
    def from_env(cls) -> "Settings":
        timeout = float(os.getenv("M2_TIMEOUT_SECONDS", "20"))
        max_chunks = int(os.getenv("M2_MAX_EVIDENCE_CHUNKS", "5"))
        max_chars = int(os.getenv("M2_MAX_EVIDENCE_CHARS", "1600"))
        if timeout <= 0 or max_chunks < 1 or max_chars < 1:
            raise ValueError("M2 timeout and evidence limits must be positive")
        return cls(
            endpoint=os.getenv("M2_ENDPOINT") or None,
            model=os.getenv("M2_MODEL") or None,
            api_key=os.getenv("M2_API_KEY") or None,
            timeout_seconds=timeout,
            max_evidence_chunks=max_chunks,
            max_evidence_chars=max_chars,
        )


class M2Error(Exception):
    """Base class for expected M2 failures."""


class NoDomainAnswersError(M2Error):
    """Raised when there is no source material to synthesize."""


class M2ConfigurationError(M2Error):
    """Raised when required model configuration is missing."""


class LLMError(M2Error):
    """Base class for model-call failures."""


class TransientLLMError(LLMError):
    """A model-call failure that is safe to retry once."""


class PermanentLLMError(LLMError):
    """A model-call failure that must not be retried."""


class ModelOutputError(TransientLLMError):
    """The model returned malformed structured output."""


class LLMClient(Protocol):
    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
    ) -> str: ...


class OllamaChatClient:
    def __init__(
        self,
        endpoint: str,
        timeout_seconds: float,
        api_key: str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.api_key = api_key

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
    ) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": model,
            "stream": False,
            "think": False,
            "format": M2_RESPONSE_SCHEMA,
            "options": {"temperature": temperature, "num_predict": 800},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(self.endpoint, json=body, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise TransientLLMError(
                "Configured model endpoint could not be reached"
            ) from error

        if response.status_code == 429 or response.status_code >= 500:
            raise TransientLLMError(
                f"Model endpoint returned transient HTTP {response.status_code}"
            )
        if response.is_error:
            raise PermanentLLMError(
                f"Model endpoint returned HTTP {response.status_code}"
            )
        try:
            content = response.json()["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("message.content is not a string")
            return content
        except (ValueError, KeyError, TypeError) as error:
            raise TransientLLMError(
                "Model endpoint returned an invalid chat response"
            ) from error


class M2Merger:
    def __init__(
        self,
        settings: Settings | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.llm_client = llm_client or self._configured_client()

    def _configured_client(self) -> LLMClient | None:
        if not self.settings.endpoint:
            return None
        return OllamaChatClient(
            endpoint=self.settings.endpoint,
            timeout_seconds=self.settings.timeout_seconds,
            api_key=self.settings.api_key,
        )

    async def merge(self, payload: M2Input) -> M2Response:
        usable = [
            answer for answer in payload.domain_answers
            if answer.answer.strip()
            or any(_evidence_text(item).strip() for item in answer.evidence)
        ]
        if not usable:
            self._log(payload, "rejected", 0, 0, 0, "no_domain_answers")
            raise NoDomainAnswersError("No domain answers available for synthesis.")

        citations = _unique_citations(
            citation for answer in usable for citation in answer.citations
        )
        started = time.monotonic()
        if len(usable) == 1:
            text = deterministic_merge(usable)
            validate_response(text, usable, payload.failures)
            self._log(
                payload, "success", 0, len(usable), len(text),
                "single_domain_passthrough", started,
            )
            return M2Response(response=text, citations=citations)

        if self.llm_client is None or not self.settings.model:
            raise M2ConfigurationError(
                "M2_ENDPOINT and M2_MODEL are required for multi-domain synthesis."
            )

        user_prompt = make_user_prompt(payload, self.settings)
        for attempt in range(MAX_RETRIES + 1):
            try:
                raw = await self.llm_client.complete(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    model=self.settings.model,
                    temperature=0,
                )
                text = _parse_model_response(raw)
                validate_response(text, usable)
                self._log(
                    payload, "success", attempt, len(usable), len(text), "passed", started
                )
                return M2Response(response=text, citations=citations)
            except (OutputValidationError, ModelOutputError) as error:
                safe_response = deterministic_merge(usable, payload.failures, payload.request)
                validate_response(safe_response, usable, payload.failures)
                logger.warning(
                    "M2 rejected unsafe model output; used deterministic source-only merge",
                    extra={
                        "m2": {
                            "request_id": payload.request_id,
                            "status": "safe_fallback",
                            "model": self.settings.model,
                            "retry_count": attempt,
                            "domain_count": len(usable),
                            "latency_ms": round((time.monotonic() - started) * 1000),
                            "output_length": len(safe_response),
                            "validation_result": "rejected",
                            "reason": str(error),
                        }
                    },
                )
                return M2Response(response=safe_response, citations=citations)
            except TransientLLMError:
                if attempt >= MAX_RETRIES:
                    self._log(
                        payload, "failed", attempt, len(usable), 0,
                        "llm_transient_failure", started,
                    )
                    raise
                await asyncio.sleep(0.1)
            except Exception:
                self._log(
                    payload, "failed", attempt, len(usable), 0,
                    "unexpected_error", started,
                )
                raise
        raise RuntimeError("M2 retry loop exited unexpectedly")

    def _log(
        self,
        payload: M2Input,
        status: str,
        retries: int,
        domain_count: int,
        output_length: int,
        validation_result: str,
        started: float | None = None,
    ) -> None:
        logger.info(
            "M2 synthesis completed",
            extra={
                "m2": {
                    "request_id": payload.request_id,
                    "status": status,
                    "model": self.settings.model,
                    "latency_ms": (
                        round((time.monotonic() - started) * 1000) if started else 0
                    ),
                    "retry_count": retries,
                    "input_domain_count": domain_count,
                    "output_length": output_length,
                    "validation_result": validation_result,
                }
            },
        )


def _parse_model_response(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):                      # strip markdown fences
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:                 # JSON object buried in extra text
        candidates.append(text[start:end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            response = ModelResponse.model_validate(parsed).response.strip()
        except (json.JSONDecodeError, ValidationError, AttributeError, TypeError, ValueError):
            continue
        if response:
            return response

    # Accept plain prose from compatible local models; grounding checks still apply.
    if text and not text.lstrip().startswith("{"):
        return text.lstrip()

    raise ModelOutputError("Model response did not match the M2 JSON schema")

def _unique_citations(citations: Any) -> list[str | dict[str, Any]]:
    unique: list[str | dict[str, Any]] = []
    seen: set[str] = set()
    for citation in citations:
        key = json.dumps(citation, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            unique.append(citation)
    return unique


def _evidence_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "content", "snippet", "quote", "passage"):
            item = value.get(key)
            if isinstance(item, str):
                return item
    return ""
