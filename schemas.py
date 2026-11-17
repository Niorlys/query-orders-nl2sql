"""Public API contracts, independent of provider and SQL execution."""

from enum import Enum
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


MAX_QUESTION_LENGTH = 1000
JsonScalar = str | int | float | bool | None


class ApiModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        allow_inf_nan=False,
        revalidate_instances="always",
    )


class QueryRequest(ApiModel):
    question: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=MAX_QUESTION_LENGTH,
        ),
    ] = Field(description="Pregunta de 1 a 1000 caracteres tras quitar espacios exteriores.")


class QueryResponse(ApiModel):
    columns: list[str] = Field(description="Nombres de columnas en el orden de cada fila.")
    rows: list[list[JsonScalar]] = Field(
        description="Celdas escalares JSON; sin objetos ni listas anidadas."
    )
    truncated: bool = Field(description="Indica si se omitieron filas por el límite de resultados.")

    @field_validator("rows", mode="before")
    @classmethod
    def require_json_scalars(cls, value):
        if isinstance(value, list):
            for row in value:
                if isinstance(row, list):
                    if any(type(cell) not in (str, int, float, bool, type(None)) for cell in row):
                        raise ValueError("Las celdas deben ser valores escalares JSON.")
        return value

    @model_validator(mode="after")
    def require_matching_width(self):
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("Cada fila debe tener tantas celdas como columnas.")
        return self


class ErrorCode(str, Enum):
    INVALID_INPUT = "invalid_input"
    OUT_OF_SCOPE = "out_of_scope"
    AMBIGUOUS_QUESTION = "ambiguous_question"
    INVALID_LLM_RESPONSE = "invalid_llm_response"
    SQL_POLICY_VIOLATION = "sql_policy_violation"
    QUERY_RESOURCE_LIMIT = "query_resource_limit"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_TIMEOUT = "provider_timeout"
    SERVICE_NOT_READY = "service_not_ready"
    INTERNAL_ERROR = "internal_error"
    HTTP_ERROR = "http_error"


class ValidationIssue(ApiModel):
    location: list[str | int]
    code: str
    message: str


class ErrorResponse(ApiModel):
    code: ErrorCode
    message: str
    details: list[ValidationIssue] = Field(default_factory=list)
