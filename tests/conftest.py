"""Tests never read local credentials or make real provider HTTP calls."""

import os

import httpx
import httpx2
import pytest

import config


@pytest.fixture(autouse=True)
def isolate_provider_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DOTENV_PATH", tmp_path / "absent.env")
    for name in list(os.environ):
        if name.upper().startswith("OPENAI_") or name.upper() in {"APP_TIMEZONE", "DATABASE_PATH"}:
            monkeypatch.delenv(name)

    def deny_network(*args, **kwargs):
        raise AssertionError("Real HTTP is disabled in tests; use MockTransport")

    async def deny_async_network(*args, **kwargs):
        deny_network()

    for library in (httpx, httpx2):
        monkeypatch.setattr(library.HTTPTransport, "handle_request", deny_network)
        monkeypatch.setattr(library.AsyncHTTPTransport, "handle_async_request", deny_async_network)
