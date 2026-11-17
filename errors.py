"""Application errors and a single Pydantic-to-JSON error response path."""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from schemas import MAX_QUESTION_LENGTH, ErrorCode, ErrorResponse, ValidationIssue


logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code = 500
    code = ErrorCode.INTERNAL_ERROR
    public_message = "Ocurrió un error interno inesperado."

    def __init__(self, internal_message: str | None = None):
        # Diagnostic text is for server logs, never the public response.
        super().__init__(internal_message or self.public_message)


class InvalidInputError(AppError):
    status_code = 422
    code = ErrorCode.INVALID_INPUT
    public_message = "La entrada no es válida. Revisa los campos indicados."


class OutOfScopeError(AppError):
    status_code = 422
    code = ErrorCode.OUT_OF_SCOPE
    public_message = "La pregunta está fuera del alcance de los datos de pedidos."


class AmbiguousQuestionError(AppError):
    status_code = 422
    code = ErrorCode.AMBIGUOUS_QUESTION
    public_message = "La pregunta es ambigua. Especifica los criterios necesarios, como el año."


class InvalidLLMResponseError(AppError):
    status_code = 502
    code = ErrorCode.INVALID_LLM_RESPONSE
    public_message = "El proveedor devolvió una respuesta no válida."


class SQLPolicyViolationError(InvalidLLMResponseError):
    code = ErrorCode.SQL_POLICY_VIOLATION
    public_message = "El SQL generado incumple la política de consultas permitidas."


class ProviderUnavailableError(AppError):
    status_code = 503
    code = ErrorCode.PROVIDER_UNAVAILABLE
    public_message = "El proveedor no está disponible temporalmente. Inténtalo más tarde."


class QueryResourceLimitError(InvalidLLMResponseError):
    code = ErrorCode.QUERY_RESOURCE_LIMIT
    public_message = "La consulta supera el presupuesto de ejecución o el tamaño permitido."


class ProviderTimeoutError(AppError):
    status_code = 504
    code = ErrorCode.PROVIDER_TIMEOUT
    public_message = "El proveedor superó el tiempo de espera."


class QueryServiceNotReadyError(AppError):
    status_code = 503
    code = ErrorCode.SERVICE_NOT_READY
    public_message = "El servicio de consultas todavía no está configurado."


class InternalServerError(AppError):
    pass


def error_response(
    error: AppError, *, details: list[ValidationIssue] | None = None,
    headers: dict[str, str] | None = None, status_code: int | None = None,
) -> JSONResponse:
    body = ErrorResponse(code=error.code, message=error.public_message, details=details or [])
    return JSONResponse(
        status_code=status_code if status_code is not None else error.status_code,
        content=body.model_dump(mode="json"), headers=headers,
    )


VALIDATION_MESSAGES = {
    "missing": "Campo obligatorio.",
    "string_type": "Se requiere una cadena de texto.",
    "string_too_short": "La pregunta no puede estar vacía.",
    "string_too_long": f"La pregunta no puede superar {MAX_QUESTION_LENGTH} caracteres.",
    "extra_forbidden": "Campo no permitido.",
    "json_invalid": "El cuerpo no contiene JSON válido.",
}


class PublicHTTPError(AppError):
    code = ErrorCode.HTTP_ERROR

    def __init__(self, status_code: int):
        self.status_code = status_code
        self.public_message = {
            400: "Solicitud HTTP no válida.", 401: "Autenticación requerida.",
            403: "Acceso no permitido.", 404: "Ruta no encontrada.",
            405: "Método HTTP no permitido.",
        }.get(status_code, "La solicitud HTTP no pudo completarse.")
        super().__init__()


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, error: AppError):
        if error.status_code == 500:
            logger.error("Application error", exc_info=(type(error), error, error.__traceback__))
        return error_response(error)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, error: RequestValidationError):
        details = [ValidationIssue(
            location=list(issue["loc"]), code=issue["type"],
            message=VALIDATION_MESSAGES.get(issue["type"], "Valor no válido para este campo."),
        ) for issue in error.errors()]
        return error_response(InvalidInputError(), details=details)

    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, error: HTTPException):
        mapped_errors = {
            422: InvalidInputError, 502: InvalidLLMResponseError,
            503: ProviderUnavailableError, 504: ProviderTimeoutError,
        }
        if error.status_code in mapped_errors:
            public_error = mapped_errors[error.status_code]()
        elif error.status_code >= 500:
            public_error = InternalServerError()
        else:
            public_error = PublicHTTPError(error.status_code)
        # Arbitrary HTTPException.detail can contain internal data: do not echo it.
        return error_response(public_error, headers=error.headers, status_code=error.status_code)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, error: Exception):
        logger.error("Unexpected request error", exc_info=(type(error), error, error.__traceback__))
        return error_response(InternalServerError())


ERROR_RESPONSES = {
    422: {"model": ErrorResponse, "description": "Entrada inválida, pregunta fuera de alcance o ambigua."},
    502: {"model": ErrorResponse, "description": "Respuesta del LLM inválida, SQL fuera de política o presupuesto excedido."},
    503: {"model": ErrorResponse, "description": "Proveedor temporalmente no disponible o servicio aún no configurado."},
    504: {"model": ErrorResponse, "description": "Timeout del proveedor."},
    500: {"model": ErrorResponse, "description": "Fallo interno; mensaje público genérico."},
    400: {"model": ErrorResponse, "description": "Solicitud HTTP inválida."},
    404: {"model": ErrorResponse, "description": "Ruta no encontrada."},
    405: {"model": ErrorResponse, "description": "Método HTTP no permitido."},
    "default": {"model": ErrorResponse, "description": "Otros errores HTTP con el mismo contrato."},
}
