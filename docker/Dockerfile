# syntax=docker/dockerfile:1

# ---- Builder stage ----
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

WORKDIR /app

ARG GIT_TAG
ARG GIT_REPO

# The devkit container binary answers the build-time pyproject questions here and is the
# entrypoint in the final stage. Pinned to a devkit-container release (its own tag
# stream); setup-project fills a missing pin and keeps an existing one.
ADD https://github.com/AetherBreaker/aeth-devkit/releases/download/container-v1/devkit-container-x86_64-unknown-linux-musl /app/devkit-container
RUN chmod +x /app/devkit-container

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
  && mv /tmp/repo/pyproject.toml /tmp/repo/uv.lock /app/

# Install all dependencies (without the project itself) using the frozen lockfile.
# This layer is cached as long as pyproject.toml/uv.lock don't change, even
# when only source code changes between deployments.
RUN --mount=type=cache,target=/root/.cache/uv \
  extras=$(/app/devkit-container app-extra) \
  && uv sync --frozen --no-dev --no-install-project $extras

# Now bring in the source tree, then the readme the wheel build reads, at the same
# relative path (`project.readme` may point into a subdirectory). The tree moves first so
# a readme inside it comes along and never pre-creates `/app/src`, which would
# make `mv` nest the tree. Only a missing readme is tolerated; a failing helper is not.
RUN mv /tmp/repo/src /app/src \
  && readme_file=$(/app/devkit-container readme) \
  && if [ -n "${readme_file}" ] && [ -f "/tmp/repo/${readme_file}" ]; then \
       mkdir -p "/app/$(dirname "${readme_file}")" \
       && mv "/tmp/repo/${readme_file}" "/app/${readme_file}"; \
     fi \
  && rm -rf /tmp/repo

# Install the project itself as a non-editable wheel so the source tree is not
# required at runtime.

RUN --mount=type=cache,target=/root/.cache/uv \
  extras=$(/app/devkit-container app-extra) \
  && uv sync --frozen --no-dev --no-editable $extras

# ---- Final stage ----
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

# Setup a non-root user. /app stays root-owned: the code is read-only to the app, which
# writes only to its mounted persisted dirs (or temp dirs).
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

# Copy artifacts needed by the entrypoint
COPY --from=builder /app/pyproject.toml /app/pyproject.toml
COPY --from=builder /app/devkit-container /app/devkit-container

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"

# The entrypoint checks every required_persisted_dir is bind-mounted, chowns them to
# nonroot, drops privileges, and execs the project's run-app-* script.
ENTRYPOINT ["/app/devkit-container", "run"]
