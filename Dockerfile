# Supply approved immutable image references using --build-arg.
ARG UV_IMAGE
ARG PYTHON_IMAGE
FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
ENV UV_LINK_MODE=copy
RUN uv sync --locked --no-dev --no-install-project

FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system api && useradd --system --gid api --home-dir /app api
WORKDIR /app
COPY --from=dependencies /app/.venv /app/.venv
COPY auth.py authorization.py authorization_models.py billing.py config.py database.py firestore_access.py health.py main.py observability.py project_sessions.py projects.py rate_limits.py request_limits.py serve.py ./
COPY conversation.py workspace_runs.py workspace_dispatch.py sandbox_worker.py ./
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER api
CMD ["python", "serve.py"]
