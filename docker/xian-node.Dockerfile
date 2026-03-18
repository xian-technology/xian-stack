# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.14-bookworm
FROM ${PYTHON_IMAGE} AS node-base

ARG COMETBFT_VERSION=0.38.12
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    XIAN_CONFIGS_DIR=/opt/xian-configs

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    procps \
    wget \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/xian

COPY --from=xian-py . /tmp/build/xian-py
COPY --from=xian-contracting . /tmp/build/xian-contracting
COPY --from=xian-abci . /tmp/build/xian-abci
COPY --from=xian-configs . /opt/xian-configs

RUN python -m pip install \
    /tmp/build/xian-py \
    /tmp/build/xian-contracting \
    /tmp/build/xian-abci \
    && rm -rf /tmp/build

RUN case "${TARGETARCH}" in \
        amd64) COMETBFT_ARCH="amd64" ;; \
        arm64) COMETBFT_ARCH="arm64" ;; \
        *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && wget -O /tmp/cometbft.tar.gz \
        "https://github.com/cometbft/cometbft/releases/download/v${COMETBFT_VERSION}/cometbft_${COMETBFT_VERSION}_linux_${COMETBFT_ARCH}.tar.gz" \
    && tar -C /usr/local/bin -xzf /tmp/cometbft.tar.gz cometbft \
    && rm -f /tmp/cometbft.tar.gz

FROM node-base AS split

WORKDIR /opt/xian
CMD ["/bin/bash"]

FROM node-base AS integrated

ARG S6_OVERLAY_VERSION=3.2.1.0
ARG TARGETARCH

RUN case "${TARGETARCH}" in \
        amd64) S6_ARCH="x86_64" ;; \
        arm64) S6_ARCH="aarch64" ;; \
        *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && wget -O /tmp/s6-overlay-noarch.tar.xz \
        "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz" \
    && wget -O /tmp/s6-overlay-noarch.tar.xz.sha256 \
        "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz.sha256" \
    && wget -O /tmp/s6-overlay-${S6_ARCH}.tar.xz \
        "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${S6_ARCH}.tar.xz" \
    && wget -O /tmp/s6-overlay-${S6_ARCH}.tar.xz.sha256 \
        "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${S6_ARCH}.tar.xz.sha256" \
    && cd /tmp \
    && sha256sum -c s6-overlay-noarch.tar.xz.sha256 \
    && sha256sum -c s6-overlay-${S6_ARCH}.tar.xz.sha256 \
    && tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz \
    && tar -C / -Jxpf /tmp/s6-overlay-${S6_ARCH}.tar.xz \
    && rm -f \
        /tmp/s6-overlay-noarch.tar.xz \
        /tmp/s6-overlay-noarch.tar.xz.sha256 \
        /tmp/s6-overlay-${S6_ARCH}.tar.xz \
        /tmp/s6-overlay-${S6_ARCH}.tar.xz.sha256

COPY docker/s6-overlay/ /

WORKDIR /opt/xian
ENTRYPOINT ["/init"]

FROM split AS dev

RUN python -m pip install pytest parameterized

WORKDIR /workspace
CMD ["/bin/bash"]
