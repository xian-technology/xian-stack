# syntax=docker/dockerfile:1.4

# Build stage
FROM python:3.13-slim AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install build dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the lock inputs plus local editable dependencies used by xian-intentkit.
COPY pyproject.toml uv.lock ./
COPY --from=xian_py . /xian-py
COPY --from=xian_accounts . /xian-contracting/packages/xian-accounts
COPY --from=xian_runtime_types . /xian-contracting/packages/xian-runtime-types

# Install dependencies with app group (excludes dev-only tools like pytest, ruff)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --group app --no-install-project

# Copy the rest of the project after dependency resolution for better caching.
COPY . .

# Install the project itself
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --group app

# Runtime stage
FROM python:3.13-slim AS runtime

# Install runtime dependencies only
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application code
COPY --from=builder /app/intentkit /app/intentkit
COPY --from=builder /app/app /app/app
COPY --from=builder /app/scripts /app/scripts

ARG RELEASE=local
ENV RELEASE=$RELEASE
ENV PATH="/app/.venv/bin:$PATH"

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "80"]
