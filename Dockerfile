# syntax=docker/dockerfile:1
#
# Stages: test -> build -> artifacts, and runtime (last, so a plain `docker build .` produces it).
#   docker build --target test .
#   docker build --target artifacts --output dist/ .
#   docker build -t opencoord:dev .

ARG UV_IMAGE=ghcr.io/astral-sh/uv:python3.13-bookworm-slim

FROM ${UV_IMAGE} AS test
WORKDIR /src
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
# Dear PyGui is imported (not run) by the UI tests that need no display.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libx11-6 libgl1 \
 && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY tests ./tests
COPY profiles ./profiles
RUN uv sync --locked
RUN uv run --no-sync ruff check \
 && uv run --no-sync ruff format --check
RUN uv run --no-sync pytest -m "not ui and not hardware"

FROM ${UV_IMAGE} AS build
# binutils: PyInstaller's binary analysis (objdump); curl + file: appimagetool download/run;
# X11/GL libs: so dearpygui imports during PyInstaller's hook analysis.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      binutils ca-certificates curl file libgl1 libx11-6 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv build --out-dir /out/dist
# Linux native bundle: PyInstaller onedir (.tar.gz) + AppImage (appimagetool --appimage-extract-and-run, no FUSE).
COPY packaging ./packaging
RUN uv sync --locked \
 && uv run --no-sync python packaging/build.py --appimage \
 && cp dist/OpenCoord-*.tar.gz dist/OpenCoord-*.AppImage /out/dist/

FROM scratch AS artifacts
COPY --from=build /out/dist/ /

FROM ${UV_IMAGE} AS runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libgl1 libglx-mesa0 libgl1-mesa-dri libegl1 libglu1-mesa \
      libx11-6 libxrandr2 libxinerama1 libxcursor1 libxi6 libxext6 libxkbcommon0 \
 && rm -rf /var/lib/apt/lists/*
RUN --mount=type=bind,from=build,source=/out/dist,target=/dist \
    uv venv /opt/venv \
 && uv pip install --python /opt/venv/bin/python /dist/*.whl
ENV PATH="/opt/venv/bin:${PATH}"
ENTRYPOINT ["opencoord"]
