import asyncio
import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from openai import AsyncOpenAI

import config
import query_service
from config import Settings
from errors import (
    InvalidLLMResponseError, ProviderTimeoutError, ProviderUnavailableError, QueryServiceNotReadyError,
)
from llm_provider import LLMDecision, OpenAIProvider, retry_after_seconds
from main import create_app
from storage import clean_to_sqlite


QUERY = "SELECT COALESCE(SUM(importe_total_centavos),0) AS total_centavos FROM ventas_por_mes_pais WHERE país='España' AND anio_mes='2025-03'"
DECISION = {"status": "query", "sql": QUERY, "reason": None}
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "orders_views.csv"


def envelope(decision=DECISION):
    text = decision if isinstance(decision, str) else json.dumps(decision)
    return {
        "id": "resp_test", "object": "response", "created_at": 0,
        "model": "gpt-6-luna", "status": "completed", "error": None,
        "output": [{"type": "message", "id": "msg_test", "role": "assistant",
                    "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]}],
    }


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.waits = []

    def now(self):
        return self.value

    async def sleep(self, delay):
        self.waits.append(delay)
        self.value += delay


def propose_with_events(events, *, clock=None, **options):
    calls = []
    clock = clock or FakeClock()

    def handle(request):
        calls.append(request)
        event = events[len(calls) - 1]
        if isinstance(event, str) and event in ("connection", "timeout"):
            error_type = httpx.ConnectError if event == "connection" else httpx.ReadTimeout
            raise error_type("simulated", request=request)
        return event

    async def run():
        settings = Settings(api_key="test-key", **options)
        async with AsyncOpenAI(api_key="test-key", max_retries=0,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))) as client:
            provider = OpenAIProvider(client, settings, clock=clock.now, sleep=clock.sleep)
            return await provider.propose("Ventas en España en marzo de 2025", date(2026, 11, 16))

    return run, calls, clock


def test_structured_request_and_response():
    run, calls, _ = propose_with_events([httpx.Response(200, json=envelope())])
    result = asyncio.run(run())
    assert result == LLMDecision(**DECISION)
    payload = json.loads(calls[0].content)
    assert payload["model"] == "gpt-6-luna"
    assert payload["text"]["format"]["strict"] is True
    schema = payload["text"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"status", "sql", "reason"}
    assert "2026-11-16" in payload["instructions"]
    assert "America/Mexico_City" in payload["instructions"]
    for text in ("SQLite", "fecha_completado", "estado='completado'", "USD", "cliente_id", "ambiguous"):
        assert text in payload["instructions"]
    assert payload["tools"] == [] and payload["store"] is False
    assert payload["input"][0]["role"] == "user"
    assert "test-key" not in payload["instructions"]


@pytest.mark.parametrize("bad", [
    "not JSON", "```json\n{}\n```", {}, {"status": "query", "sql": "", "reason": None},
    {"status": "query", "sql": QUERY, "reason": "ambiguous"},
    {"status": "unanswerable", "sql": QUERY, "reason": "out_of_scope"},
    {"status": "unanswerable", "sql": None, "reason": None},
    {**DECISION, "extra": "no"}, {**DECISION, "sql": 123},
])
def test_malformed_content_never_retries(bad):
    run, calls, clock = propose_with_events([httpx.Response(200, json=envelope(bad))])
    with pytest.raises(InvalidLLMResponseError):
        asyncio.run(run())
    assert len(calls) == 1 and clock.waits == []


@pytest.mark.parametrize("kind", ["incomplete", "refusal", "empty", "tool_call"])
def test_invalid_envelopes_never_retry(kind):
    data = envelope()
    if kind == "incomplete":
        data["status"] = "incomplete"
    elif kind == "refusal":
        data["output"][0]["content"] = [{"type": "refusal", "refusal": "No"}]
    elif kind == "empty":
        data["output"] = []
    else:
        data["output"] = [{"type": "function_call", "call_id": "1", "name": "unsafe", "arguments": "{}"}]
    run, calls, _ = propose_with_events([httpx.Response(200, json=data)])
    with pytest.raises(InvalidLLMResponseError):
        asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize("event", ["connection", "timeout", 500, 502, 503, 429, 408])
def test_transient_failure_retries_then_succeeds(event):
    failure = httpx.Response(event, json={"error": {"message": "temporary"}}) if isinstance(event, int) else event
    run, calls, clock = propose_with_events([failure, httpx.Response(200, json=envelope())])
    assert asyncio.run(run()).status == "query"
    assert len(calls) == 2 and clock.waits == [0.5]


def test_retry_count_is_bounded_and_sdk_does_not_multiply_attempts():
    run, calls, clock = propose_with_events(["connection"] * 3)
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(run())
    assert len(calls) == 3 and clock.waits == [0.5, 1.0]


def test_no_retry_when_configured_zero():
    run, calls, clock = propose_with_events(["connection"], max_retries=0)
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(run())
    assert len(calls) == 1 and clock.waits == []


def test_backoff_consumes_total_budget():
    run, calls, clock = propose_with_events(["connection", "connection"], total_timeout_seconds=0.7)
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(run())
    assert len(calls) == 2 and clock.waits == [0.5]


def test_non_json_provider_envelope_is_not_retried():
    run, calls, clock = propose_with_events([httpx.Response(200, text="not JSON", headers={"content-type": "application/json"})])
    with pytest.raises(InvalidLLMResponseError):
        asyncio.run(run())
    assert len(calls) == 1 and clock.waits == []


def test_timeout_retried_at_most_once_even_with_higher_retry_limit():
    run, calls, clock = propose_with_events(["timeout", "timeout"], max_retries=5)
    with pytest.raises(ProviderTimeoutError):
        asyncio.run(run())
    assert len(calls) == 2 and clock.waits == [0.5]


def test_rate_limit_wait_is_respected():
    run, calls, clock = propose_with_events([
        httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {"message": "wait"}}),
        httpx.Response(200, json=envelope()),
    ])
    asyncio.run(run())
    assert len(calls) == 2 and clock.waits == [2]


def test_rate_limit_wait_that_does_not_fit_is_not_shortened():
    run, calls, clock = propose_with_events([
        httpx.Response(429, headers={"Retry-After": "31"}, json={"error": {"message": "wait"}}),
    ])
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(run())
    assert len(calls) == 1 and clock.waits == []


@pytest.mark.parametrize("status", [401, 403, 404])
def test_invalid_credentials_model_or_url_never_retry(status):
    run, calls, clock = propose_with_events([httpx.Response(status, json={"error": {"message": "secret"}})])
    with pytest.raises(QueryServiceNotReadyError):
        asyncio.run(run())
    assert len(calls) == 1 and clock.waits == []


def test_retry_after_header_formats():
    now = datetime(2026, 11, 16, tzinfo=timezone.utc)
    assert retry_after_seconds({"retry-after": "Mon, 16 Nov 2026 00:00:03 GMT"}, now) == 3
    assert retry_after_seconds({"retry-after-ms": "500"}, now) == 0.5
    assert retry_after_seconds({"retry-after": "nan"}, now) is None
    assert retry_after_seconds({"retry-after": "bad"}, now) is None


def test_total_deadline_cancels_a_slow_provider():
    calls = []

    async def slow(request):
        calls.append(request)
        await asyncio.sleep(1)
        return httpx.Response(200, json=envelope())

    async def run():
        settings = Settings(api_key="test-key", timeout_seconds=1, total_timeout_seconds=0.01)
        async with AsyncOpenAI(api_key="test-key", max_retries=0,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(slow))) as client:
            await OpenAIProvider(client, settings).propose("Ventas", date(2026, 11, 16))

    with pytest.raises(ProviderTimeoutError):
        asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize(("decision", "http_status", "code"), [
    (DECISION, 200, None),
    ({"status": "query", "sql": "DELETE FROM pedidos", "reason": None}, 502, "sql_policy_violation"),
    ({"status": "query", "sql": "SELECT * FROM pedidos_rechazados", "reason": None}, 502, "sql_policy_violation"),
    ({"status": "unanswerable", "sql": None, "reason": "out_of_scope"}, 422, "out_of_scope"),
    ({"status": "unanswerable", "sql": None, "reason": "ambiguous"}, 422, "ambiguous_question"),
    ("bad JSON", 502, "invalid_llm_response"),
])
def test_endpoint_through_mocked_sdk_and_real_safe_executor(tmp_path, monkeypatch, decision, http_status, code):
    database_path = tmp_path / "orders.sqlite3"
    clean_to_sqlite(FIXTURE_PATH, database_path)
    original_bytes = database_path.read_bytes()
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("OPENAI_BASE_URL", "https://provider.example/v1")
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=envelope(decision))

    def make_client(**options):
        assert options["max_retries"] == 0  # Only the adapter owns retries.
        assert options["base_url"] == "https://provider.example/v1"
        return AsyncOpenAI(**options, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))

    monkeypatch.setattr(query_service, "AsyncOpenAI", make_client)
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = client.post("/query", json={"question": "Ventas en España en marzo de 2025"})
    assert response.status_code == http_status, response.text
    if http_status == 200:
        assert response.json() == {"columns": ["total_centavos"], "rows": [[24000]], "truncated": False}
    else:
        assert response.json()["code"] == code
    assert len(calls) == 1
    assert database_path.read_bytes() == original_bytes


def test_settings_dotenv_environment_precedence_and_secret_repr(tmp_path, monkeypatch):
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("OPENAI_API_KEY=dotenv-secret\nOPENAI_MODEL=gpt-6-luna\nOPENAI_MAX_RETRIES=1\n")
    monkeypatch.setattr(config, "DOTENV_PATH", dotenv_path)
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")
    settings = Settings.from_env()
    assert settings.api_key.get_secret_value() == "environment-secret"
    assert settings.max_retries == 1
    assert settings.model == "gpt-6-luna"
    assert "environment-secret" not in repr(settings)


def test_settings_can_be_passed_directly_and_override_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    monkeypatch.setenv("OPENAI_MODEL", "environment-model")
    settings = Settings(api_key="direct-key", model=" gpt-6-luna ", database_path="custom.sqlite3")
    assert settings.api_key.get_secret_value() == "direct-key"
    assert settings.model == "gpt-6-luna"
    assert settings.database_path == config.PROJECT_ROOT / "custom.sqlite3"


@pytest.mark.parametrize(("name", "value"), [
    ("OPENAI_API_KEY", ""), ("OPENAI_MODEL", ""), ("OPENAI_BASE_URL", "not-url"),
    ("OPENAI_TIMEOUT_SECONDS", "-1"), ("OPENAI_TOTAL_TIMEOUT_SECONDS", "nan"),
    ("OPENAI_MAX_RETRIES", "-1"), ("APP_TIMEZONE", "not-a-zone"),
])
def test_invalid_settings_fail_without_provider_call(monkeypatch, name, value):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setenv(name, value)
    with pytest.raises(QueryServiceNotReadyError):
        Settings.from_env()


@pytest.mark.parametrize(("event", "status", "code"), [
    ("connection", 503, "provider_unavailable"), ("timeout", 504, "provider_timeout"),
    (429, 503, "provider_unavailable"), (503, 503, "provider_unavailable"),
    (401, 503, "service_not_ready"),
])
def test_sdk_failures_reach_http_error_contract(monkeypatch, event, status, code):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setenv("OPENAI_MAX_RETRIES", "0")
    calls = []

    def handle(request):
        calls.append(request)
        if event == "connection":
            raise httpx.ConnectError("secret network details", request=request)
        if event == "timeout":
            raise httpx.ReadTimeout("secret timeout details", request=request)
        return httpx.Response(event, json={"error": {"message": "secret provider details"}})

    monkeypatch.setattr(query_service, "AsyncOpenAI", lambda **kwargs: AsyncOpenAI(
        **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ))
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = client.post("/query", json={"question": "Ventas en marzo de 2025"})
    assert response.status_code == status
    assert response.json()["code"] == code
    assert set(response.json()) == {"code", "message", "details"}
    assert "secret" not in response.text
    assert len(calls) == 1


def test_invalid_question_is_422_even_when_credentials_missing():
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        assert client.post("/query", json={"question": " "}).status_code == 422
        assert client.post("/query", json={"question": "Ventas"}).status_code == 503
