"""FastAPI entry point. This stage defines contracts; no provider or SQL runs yet."""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, FastAPI

from errors import ERROR_RESPONSES, QueryServiceNotReadyError, install_error_handlers
from schemas import QueryRequest, QueryResponse


QueryService = Callable[[str], Awaitable[QueryResponse]]


async def unconfigured_query_service(question: str) -> QueryResponse:
    raise QueryServiceNotReadyError()


def get_query_service() -> QueryService:
    return unconfigured_query_service


def create_app() -> FastAPI:
    application = FastAPI(
        title="Orders Query API", version="0.1.0", debug=False,
        description="Contratos de consultas de pedidos. El LLM y la ejecución SQL aún no están conectados.",
        responses=ERROR_RESPONSES,
    )
    install_error_handlers(application)

    @application.post("/query", response_model=QueryResponse, summary="Consultar pedidos en lenguaje natural")
    async def query_orders(
        payload: QueryRequest, service: Annotated[QueryService, Depends(get_query_service)],
    ) -> QueryResponse:
        return await service(payload.question)

    return application


app = create_app()
