"""Small Responses adapter with structured decisions and one bounded retry loop."""

import asyncio
import json
import math
import time
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Literal

from openai import APIConnectionError, APIResponseValidationError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from config import Settings
from errors import (
    InternalServerError, InvalidLLMResponseError, ProviderTimeoutError,
    ProviderUnavailableError, QueryServiceNotReadyError,
)
from llm_prompt import build_instructions


class LLMDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["query", "unanswerable"]
    sql: str | None = Field(max_length=16384)
    reason: Literal["out_of_scope", "ambiguous"] | None

    @model_validator(mode="after")
    def check_consistency(self):
        if self.status == "query":
            if not self.sql or not self.sql.strip() or self.reason is not None:
                raise ValueError("Query decisions require SQL and no rejection reason")
        elif self.sql is not None or self.reason is None:
            raise ValueError("Unanswerable decisions require a reason and no SQL")
        return self


def retry_after_seconds(headers, now: datetime) -> float | None:
    """Support Retry-After seconds/HTTP-date and the provider's millisecond header."""
    try:
        if "retry-after-ms" in headers:
            value = float(headers["retry-after-ms"]) / 1000
        elif "retry-after" in headers:
            raw = headers["retry-after"]
            try:
                value = float(raw)
            except ValueError:
                target = parsedate_to_datetime(raw)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                value = (target - now).total_seconds()
        else:
            return None
        return max(0.0, value) if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def parse_decision(response) -> LLMDecision:
    try:
        if response.status != "completed" or response.error is not None:
            raise ValueError("Provider response is incomplete or failed")
        texts = []
        for item in response.output:
            if item.type == "reasoning":
                continue
            if item.type != "message" or item.role != "assistant" or item.status != "completed":
                raise ValueError("Unexpected provider output item")
            for content in item.content:
                if content.type != "output_text" or type(content.text) is not str:
                    raise ValueError("Refusal or non-text output")
                texts.append(content.text)
        if len(texts) != 1 or len(texts[0].encode("utf-8")) > 32768:
            raise ValueError("Missing, multiple, or oversized structured outputs")
        return LLMDecision.model_validate_json(texts[0])
    except (ValidationError, ValueError, TypeError, AttributeError, UnicodeError) as error:
        raise InvalidLLMResponseError("Malformed structured provider response") from error


class OpenAIProvider:
    def __init__(self, client: AsyncOpenAI, settings: Settings, *, clock=time.monotonic,
                 sleep=asyncio.sleep, wall_clock=lambda: datetime.now(timezone.utc)):
        self.client = client
        self.settings = settings
        self.clock, self.sleep, self.wall_clock = clock, sleep, wall_clock

    async def propose(self, question: str, reference_date: date) -> LLMDecision:
        deadline = self.clock() + self.settings.total_timeout_seconds
        timeout_retries = 0
        for attempt in range(self.settings.max_retries + 1):
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise ProviderTimeoutError("Total provider deadline exceeded")
            attempt_timeout = min(remaining, self.settings.timeout_seconds)
            headers = {}
            is_timeout = False
            try:
                async with asyncio.timeout(attempt_timeout):
                    response = await self.client.responses.create(
                        model=self.settings.model,
                        instructions=build_instructions(reference_date, self.settings.timezone),
                        input=[{"role": "user", "content": question}],
                        text={"format": {"type": "json_schema", "name": "query_decision",
                                         "strict": True, "schema": LLMDecision.model_json_schema()}},
                        max_output_tokens=4096, reasoning={"effort": "low"},
                        store=False, tools=[], timeout=attempt_timeout,
                    )
            except (APITimeoutError, TimeoutError):
                failure = ProviderTimeoutError("Provider attempt timed out")
                is_timeout = True
            except APIConnectionError:
                failure = ProviderUnavailableError("Temporary provider connection error")
            except (APIResponseValidationError, json.JSONDecodeError) as error:
                raise InvalidLLMResponseError("Invalid provider envelope") from error
            except APIStatusError as error:
                if error.status_code in (401, 403, 404):
                    raise QueryServiceNotReadyError("Provider credentials, access, model or URL invalid") from error
                if error.status_code == 408:
                    failure = ProviderTimeoutError("Provider returned HTTP 408")
                    is_timeout = True
                elif error.status_code in (409, 429) or error.status_code >= 500:
                    failure = ProviderUnavailableError("Temporary provider HTTP failure")
                else:
                    raise InternalServerError("Provider rejected the API request configuration") from error
                headers = error.response.headers
            else:
                if self.clock() >= deadline:
                    raise ProviderTimeoutError("Total provider deadline exceeded")
                # Parse outside the retry path: never regenerate malformed content.
                return parse_decision(response)

            if attempt == self.settings.max_retries or (is_timeout and timeout_retries >= 1):
                raise failure
            delay = retry_after_seconds(headers, self.wall_clock())
            if delay is None:
                delay = min(0.5 * 2**attempt, 4.0)
            remaining = deadline - self.clock()
            # Reserve a small useful window for the next attempt; do not shorten Retry-After.
            if delay + 0.01 >= remaining:
                raise failure
            if is_timeout:
                timeout_retries += 1
            try:
                async with asyncio.timeout(remaining):
                    await self.sleep(delay)
            except TimeoutError as error:
                raise ProviderTimeoutError("Total deadline exceeded while waiting to retry") from error
        raise InternalServerError("Unreachable retry state")
