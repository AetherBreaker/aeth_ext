# syntax=docker/dockerfile:1

# ---- Builder stage ----
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

WORKDIR /app

ARG PACKAGE_NAME
ARG PACKAGE_VERSION
ARG SFTPYPI_INDEX_URL=https://pypi.sweetfiretobacco.com/jacob.ogden/internal/+simple
ARG PYPI_INDEX_URL=https://pypi.org/simple
ARG UV_INDEX_STRATEGY=unsafe-best-match

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Install git (required for uv to fetch git-based dependencies)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Use the repository pyproject to resolve and install dependencies into the builder venv.
COPY pyproject.toml /app/pyproject.toml

RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /app/.venv \
    && uv pip install --python /app/.venv/bin/python \
    --index-url ${SFTPYPI_INDEX_URL} \
    --extra-index-url ${PYPI_INDEX_URL} \
    --index-strategy ${UV_INDEX_STRATEGY} \
    ${PACKAGE_NAME}==${PACKAGE_VERSION}

# ---- Final stage ----
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

# Setup a non-root user
RUN groupadd --system --gid 999 nonroot \
    && useradd --system --gid 999 --uid 999 --create-home nonroot

WORKDIR /app

# Prevents Python from writing pyc files.
ENV PYTHONDONTWRITEBYTECODE=1
# Keeps Python from buffering stdout and stderr to avoid situations where
# the application crashes without emitting any logs due to buffering.
ENV PYTHONUNBUFFERED=1
# Enable Python optimizations (removes assert statements and sets __debug__ to False)
ENV PYTHONOPTIMIZE=1

# Copy the virtual environment from the builder stage
COPY --from=builder /app/.venv /app/.venv

# Create runtime writable directories and grant ownership to the non-root user.
RUN mkdir -p /app/persisted_data \
    && chown -R 999:999 /app/persisted_data

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"

# Reset the image entrypoint so we can explicitly invoke uv in CMD
ENTRYPOINT []

# Use the non-root user to run our application
USER nonroot

# Run the application.
WORKDIR /app
CMD ["uv", "run", "-m", "aeth_ext.shared_log_processor"]