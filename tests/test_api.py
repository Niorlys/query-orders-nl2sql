import json
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from errors import (
    AmbiguousQuestionError, AppError, InternalServerError, InvalidInputError,
    InvalidLLMResponseError, OutOfScopeError, ProviderTimeoutError,
    ProviderUnavailableError, QueryServiceNotReadyError, SQLPolicyViolationError,
)
from main import create_app, get_query_service
from schemas import ErrorResponse, MAX_QUESTION_LENGTH, QueryRequest, QueryResponse


@pytest.fixture
def application():
    return create_app()


@pytest.fixture
def client(application):
    with TestClient(application, raise_server_exceptions=False) as test_client:
        yield test_client


def assert_error(response, status, code):
    assert response.status_code == status
    assert response.headers["content-type"] == "application/json"
    parsed = ErrorResponse.model_validate_json(response.content)
    assert parsed.code.value == code
    assert set(response.json()) == {"code", "message", "details"}
    assert parsed.message
    return parsed


@pytest.mark.parametrize(("error_type", "status", "code"), [
    (InvalidInputError, 422, "invalid_input"),
    (OutOfScopeError, 422, "out_of_scope"),
    (AmbiguousQuestionError, 422, "ambiguous_question"),
    (InvalidLLMResponseError, 502, "invalid_llm_response"),
    (SQLPolicyViolationError, 502, "sql_policy_violation"),
    (ProviderUnavailableError, 503, "provider_unavailable"),
    (ProviderTimeoutError, 504, "provider_timeout"),
    (QueryServiceNotReadyError, 503, "service_not_ready"),
    (InternalServerError, 500, "internal_error"),
    (AppError, 500, "internal_error"),
])
def test_application_error_categories(application, client, error_type, status, code):
    async def fake_service(question):
        raise error_type("secret provider payload /internal/path")

    application.dependency_overrides[get_query_service] = lambda: fake_service
    parsed = assert_error(client.post("/query", json={"question": "Ventas de 2025"}), status, code)
    assert parsed.details == []
    assert "secret" not in parsed.message
    assert "/internal/path" not in parsed.message


@pytest.mark.parametrize("payload", [
    {}, {"question": ""}, {"question": " \t\n "}, {"question": "\u00a0"},
    {"question": "a" * (MAX_QUESTION_LENGTH + 1)}, {"question": None},
    {"question": 12}, {"question": True}, {"question": []},
    {"question": "Ventas", "sql": "SELECT * FROM pedidos"}, [], "Ventas",
])
def test_invalid_input_is_rejected_before_service(application, client, payload):
    calls = []

    async def fake_service(question):
        calls.append(question)
        return QueryResponse(columns=[], rows=[], truncated=False)

    application.dependency_overrides[get_query_service] = lambda: fake_service
    parsed = assert_error(client.post("/query", json=payload), 422, "invalid_input")
    assert parsed.details
    assert calls == []
    assert all(set(detail.model_dump()) == {"location", "code", "message"} for detail in parsed.details)


def test_missing_body_and_malformed_json(client):
    assert_error(client.post("/query"), 422, "invalid_input")
    response = client.post("/query", content='{"question": secret}', headers={"Content-Type": "application/json"})
    parsed = assert_error(response, 422, "invalid_input")
    assert parsed.details[0].code == "json_invalid"
    assert "secret" not in response.text


def test_default_endpoint_is_explicitly_not_ready(client):
    parsed = assert_error(client.post("/query", json={"question": "Ventas de 2025"}), 503, "service_not_ready")
    assert "todavía no está configurado" in parsed.message


def test_successful_contract_with_simulated_service(application, client):
    calls = []

    async def fake_service(question):
        calls.append(question)
        return QueryResponse(
            columns=["country", "amount_cents", "ratio", "active", "optional"],
            rows=[["España", 12000, 0.5, True, None]], truncated=True,
        )

    application.dependency_overrides[get_query_service] = lambda: fake_service
    response = client.post("/query", json={"question": "  Ventas en España  "})
    assert response.status_code == 200
    assert calls == ["Ventas en España"]
    assert response.json() == {
        "columns": ["country", "amount_cents", "ratio", "active", "optional"],
        "rows": [["España", 12000, 0.5, True, None]], "truncated": True,
    }
    assert QueryRequest(question="a" * MAX_QUESTION_LENGTH).question == "a" * MAX_QUESTION_LENGTH


@pytest.mark.parametrize(("method", "path", "status"), [("get", "/missing", 404), ("get", "/query", 405)])
def test_framework_http_errors_share_contract(client, method, path, status):
    response = getattr(client, method)(path)
    assert_error(response, status, "http_error")
    if status == 405:
        assert response.headers["allow"] == "POST"


@pytest.mark.parametrize("exception_type", [HTTPException, StarletteHTTPException])
@pytest.mark.parametrize(("status", "code"), [
    (400, "http_error"), (403, "http_error"), (422, "invalid_input"),
    (500, "internal_error"), (502, "invalid_llm_response"),
    (503, "provider_unavailable"), (504, "provider_timeout"),
])
def test_http_exceptions_are_serialized_and_sanitized(application, client, exception_type, status, code):
    async def fake_service(question):
        raise exception_type(status_code=status, detail="secret internals", headers={"X-Test": "preserved"})

    application.dependency_overrides[get_query_service] = lambda: fake_service
    response = client.post("/query", json={"question": "Ventas"})
    assert_error(response, status, code)
    assert response.headers["x-test"] == "preserved"
    assert "secret" not in response.text


def test_unexpected_error_returns_generic_message(application, client, caplog):
    async def fake_service(question):
        raise RuntimeError("secret database path")

    application.dependency_overrides[get_query_service] = lambda: fake_service
    response = client.post("/query", json={"question": "Ventas"})
    assert_error(response, 500, "internal_error")
    assert response.json()["message"] == "Ocurrió un error interno inesperado."
    assert "secret" not in response.text
    assert "secret database path" in caplog.text


@pytest.mark.parametrize("result", [
    {"columns": ["amount"], "rows": [[{"invalid": "secret"}]], "truncated": False},
    {"columns": ["amount"], "rows": [[1, 2]], "truncated": False},
    {"columns": ["amount"], "rows": [[float("nan")]], "truncated": False},
])
def test_invalid_internal_response_is_500_not_request_validation_422(application, client, result):
    async def fake_service(question):
        return result

    application.dependency_overrides[get_query_service] = lambda: fake_service
    response = client.post("/query", json={"question": "Ventas"})
    assert_error(response, 500, "internal_error")
    assert "secret" not in response.text


def test_mutated_response_model_is_revalidated(application, client):
    async def fake_service(question):
        result = QueryResponse(columns=["value"], rows=[[1]], truncated=False)
        result.rows[0][0] = {"secret": "invalid scalar"}
        return result

    application.dependency_overrides[get_query_service] = lambda: fake_service
    response = client.post("/query", json={"question": "Ventas"})
    assert_error(response, 500, "internal_error")
    assert "secret" not in response.text


@pytest.mark.parametrize("cell", [
    {}, [], [1], {1, 2}, b"bytes", date(2025, 1, 1), Decimal("1.23"),
    float("nan"), float("inf"), -float("inf"),
])
def test_response_rejects_non_json_scalars(cell):
    with pytest.raises(ValidationError):
        QueryResponse(columns=["value"], rows=[[cell]], truncated=False)


@pytest.mark.parametrize("payload", [
    {"columns": ["one"], "rows": [[1, 2]], "truncated": False},
    {"columns": ["one"], "rows": [[1]], "truncated": "false"},
    {"columns": ["one"], "rows": [[1]], "truncated": 0},
    {"columns": [1], "rows": [[1]], "truncated": False},
    {"columns": [], "rows": [], "truncated": False, "extra": "no"},
])
def test_response_rejects_invalid_shape_or_types(payload):
    with pytest.raises(ValidationError):
        QueryResponse(**payload)


def test_empty_result_and_scalar_types_are_preserved():
    assert QueryResponse(columns=["value"], rows=[], truncated=False).rows == []
    cells = ["12", 12, 0.5, True, None]
    result = QueryResponse(columns=["a", "b", "c", "d", "e"], rows=[cells], truncated=False)
    decoded = json.loads(result.model_dump_json())["rows"][0]
    assert [type(cell) for cell in decoded] == [str, int, float, bool, type(None)]


def test_openapi_documents_all_contracts_and_error_categories(client):
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/query"]["post"]
    models = schema["components"]["schemas"]
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/QueryRequest")
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/QueryResponse")
    for status in ("422", "500", "502", "503", "504", "400", "404", "405", "default"):
        assert operation["responses"][status]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
    assert "HTTPValidationError" not in models
    assert models["QueryRequest"]["properties"]["question"]["minLength"] == 1
    assert models["QueryRequest"]["properties"]["question"]["maxLength"] == MAX_QUESTION_LENGTH
    assert models["QueryRequest"]["additionalProperties"] is False
    scalar_types = models["QueryResponse"]["properties"]["rows"]["items"]["items"]["anyOf"]
    assert {item["type"] for item in scalar_types} == {"string", "integer", "number", "boolean", "null"}
