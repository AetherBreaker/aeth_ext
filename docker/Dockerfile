# syntax=docker/dockerfile:1

# ---- Builder stage ----
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

WORKDIR /app

ARG GIT_TAG
ARG GIT_REPO

# Copy helper scripts used during the build
COPY docker/scripts/ /app/scripts/
# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Install git (required for uv to fetch git-based dependencies)
RUN apt-get update && apt-get install -y --no-install-recommends git \
  && rm -rf /var/lib/apt/lists/*

# Clone only the dependency manifest files first so the dep install layer
# can be cached independently of source code changes.
RUN git clone --depth 1 --branch "${GIT_TAG}" "${GIT_REPO}" /tmp/repo \
  && mv /tmp/repo/pyproject.toml /tmp/repo/uv.lock /app/ \
  && readme_file=$(uv run --no-project python /app/scripts/get_readme.py 2>/dev/null) \
  && [ -n "${readme_file}" ] && [ -f "/tmp/repo/${readme_file}" ] && mv "/tmp/repo/${readme_file}" /app/ || true

# Install all dependencies (without the project itself) using the frozen lockfile.
# This layer is cached as long as pyproject.toml/uv.lock don't change, even
# when only source code changes between deployments.
RUN --mount=type=cache,target=/root/.cache/uv \
  extras=$(uv run --no-project python /app/scripts/detect_app_extra.py) \
  && uv sync --frozen --no-dev --no-install-project $extras

# Now bring in the source tree and install the project itself as a
# non-editable wheel so the source tree is not required at runtime.
RUN mv /tmp/repo/src /app/src && rm -rf /tmp/repo

RUN --mount=type=cache,target=/root/.cache/uv \
  extras=$(uv run --no-project python /app/scripts/detect_app_extra.py) \
  && uv sync --frozen --no-dev --no-editable $extras

# ---- Final stage ----
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

# Setup a non-root user and install gosu for privilege dropping in the entrypoint
RUN groupadd --system --gid 999 nonroot \
  && useradd --system --gid 999 --uid 999 --create-home nonroot

# install gosu for privilege dropping in the entrypoint
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
  && rm -rf /var/lib/apt/lists/*

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

# Copy artifacts needed by the entrypoint
COPY --from=builder /app/pyproject.toml /app/pyproject.toml
COPY --from=builder /app/scripts/ /app/scripts/
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"

# Run the application.
WORKDIR /app
# The entrypoint chowns volume paths then drops to nonroot via gosu.
ENTRYPOINT ["/app/entrypoint.sh"]

