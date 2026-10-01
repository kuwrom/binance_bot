# Base image and uv are pinned by digest; dependencies come only from uv.lock. Bumping: deploy/DEPLOY.md.
FROM python:3.13.15-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b
COPY --from=ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 /uv /uvx /bin/
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=0
WORKDIR /app
# --locked: install exactly uv.lock (hash-checked) and fail if it is stale against pyproject.toml.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project
COPY src ./src
RUN uv sync --locked --no-editable
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd -m bot && mkdir -p /app/data && chown bot /app/data
USER bot
CMD ["shortbot-guardian"]
