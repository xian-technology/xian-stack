FROM python:3.14-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.25 /uv /uvx /bin/

RUN apt-get update && apt-get install -y \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app

RUN uv pip install --system pytest parameterized

CMD ["tail", "-f", "/dev/null"]
