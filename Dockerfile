# syntax=docker/dockerfile:1

# ---- Builder stage ----
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

WORKDIR /app

ARG GIT_TAG
ARG GIT_REPO

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Install git (required for uv to fetch git-based dependencies)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Clone the repository at the pinned tag.
RUN git clone --depth 1 --branch "${GIT_TAG}" "${GIT_REPO}" /tmp/repo && \
    mv /tmp/repo/pyproject.toml /tmp/repo/uv.lock /tmp/repo/README.md /app/ && \
    mv /tmp/repo/src /app/src && \
    rm -rf /tmp/repo

# Install all dependencies (without the project itself) using the frozen lockfile.
# No live resolution occurs — exact versions come from uv.lock, so no
# --prerelease flag is needed and no unexpected pre-releases can sneak in.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra logserver

# Install the project itself as a non-editable wheel so the
# source tree is not required at runtime.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

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
CMD ["python", "-m", "aeth_ext.central_log_server"]