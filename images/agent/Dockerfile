FROM ghcr.io/astral-sh/uv:python3.11-trixie-slim@sha256:015fe7b33cea4a9a0a2fcda085cbfa3fa03419f973ff1b6a3e337edde48ba9ff

ARG DEBIAN_FRONTEND=noninteractive

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

RUN uv pip install --system build123d==0.10.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git jq libgl1 libxrender1 ripgrep sudo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
