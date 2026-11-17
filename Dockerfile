FROM python:3.13.12-slim-bookworm@sha256:a58daefb915e1e03ad48f3ca4df8832065412c5c35cacb9d39f4229184de12b6

COPY --from=ghcr.io/astral-sh/uv:0.10.4 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY requirements.txt constraints.txt ./
RUN uv pip install --system -r requirements.txt \
    && uv pip check --system

# .dockerignore only allows the application source and dependency files.
COPY *.py views.sql ./

RUN mkdir -p /app/data && chown 10001:10001 /app/data
USER 10001:10001

EXPOSE 8000

# Startup never generates data or loads/replaces SQLite.
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
