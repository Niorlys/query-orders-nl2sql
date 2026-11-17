"""FastAPI entry point for natural-language questions and validated SQLite queries."""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI

from errors import ERROR_RESPONSES, install_error_handlers
from query_service import run_question
from schemas import QueryRequest, QueryResponse


QueryService = Callable[[str], Awaitable[QueryResponse]]
router = APIRouter()


def get_query_service() -> QueryService:
    return run_question


@router.post("/query", response_model=QueryResponse, summary="Consultar pedidos en lenguaje natural")
async def query_orders(
    payload: QueryRequest, service: Annotated[QueryService, Depends(get_query_service)],
) -> QueryResponse:
    return await service(payload.question)


def create_app() -> FastAPI:
    application = FastAPI(
        title="Orders Query API", version="0.1.0", debug=False,
        description="Preguntas de pedidos convertidas a SQL validado y ejecutado en SQLite de solo lectura.",
        responses=ERROR_RESPONSES,
    )
    install_error_handlers(application)
    application.include_router(router)
    return application


app = create_app()
