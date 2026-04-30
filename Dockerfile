FROM node:22-trixie-slim AS node_runtime

ENV NPM_CONFIG_UPDATE_NOTIFIER=false

RUN npm install -g @openai/codex@0.117.0-alpha.3 @mariozechner/pi-coding-agent@0.54.0 pi-web-access@0.10.6 \
    && npm cache clean --force \
    && rm -rf /root/.npm

FROM ghcr.io/astral-sh/uv:python3.11-trixie-slim

ARG DEBIAN_FRONTEND=noninteractive

ENV PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends blender ca-certificates git libxft2 libxinerama1 ripgrep sudo \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node_runtime /usr/local/bin/ /usr/local/bin/
COPY --from=node_runtime /usr/local/lib/node_modules/ /usr/local/lib/node_modules/

WORKDIR /app

COPY README.md pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

COPY . .
RUN uv sync --locked

ENTRYPOINT ["uv", "run", "--no-sync"]
CMD ["pytest"]
