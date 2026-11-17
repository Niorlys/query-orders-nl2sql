"""Connect the provider decision to the mandatory read-only SQL execution layer."""

from datetime import datetime
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI
from starlette.concurrency import run_in_threadpool

from config import Settings
from errors import AmbiguousQuestionError, OutOfScopeError
from llm_provider import OpenAIProvider
from query_executor import execute_query
from schemas import QueryResponse


async def answer_question(
    question: str, provider: OpenAIProvider, settings: Settings
) -> QueryResponse:
    reference_date = datetime.now(ZoneInfo(settings.timezone)).date()
    decision = await provider.propose(question, reference_date)
    if decision.status == "unanswerable":
        if decision.reason == "ambiguous":
            raise AmbiguousQuestionError()
        raise OutOfScopeError()
    # execute_query always validates before opening SQLite. No other SQL execution path.
    return await run_in_threadpool(execute_query, decision.sql, settings.database_path)


async def run_question(question: str) -> QueryResponse:
    # Lazy configuration: request validation runs first, even without credentials.
    settings = Settings.from_env()
    async with AsyncOpenAI(
        api_key=settings.api_key.get_secret_value(),
        base_url=str(settings.base_url),
        timeout=settings.timeout_seconds,
        max_retries=0,
    ) as client:
        # SDK retries are off to avoid multiplying this adapter's explicit retry policy.
        return await answer_question(question, OpenAIProvider(client, settings), settings)
