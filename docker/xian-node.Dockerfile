# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.14-bookworm

FROM golang:1.22-bookworm AS cometbft-builder

ARG COMETBFT_VERSION=0.38.21
ARG TARGETOS=linux
ARG TARGETARCH

ENV CGO_ENABLED=0

RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    GOOS="${TARGETOS}" GOARCH="${TARGETARCH}" GOBIN=/out \
    go install -trimpath -buildvcs=false \
    "github.com/cometbft/cometbft/cmd/cometbft@v${COMETBFT_VERSION}"

FROM ${PYTHON_IMAGE} AS python-wheel-builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

ENV PATH=/root/.cargo/bin:${PATH}

RUN curl https://sh.rustup.rs -sSf | sh -s -- -y --profile minimal --default-toolchain stable

WORKDIR /tmp/build

COPY --from=xian-py . /tmp/build/xian-py
COPY --from=xian-contracting . /tmp/build/xian-contracting
COPY --from=xian-abci . /tmp/build/xian-abci

RUN python -m pip install --upgrade pip wheel maturin \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-contracting/packages/xian-runtime-types \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-contracting/packages/xian-native-tracer \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-py \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-contracting \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-abci

FROM ${PYTHON_IMAGE} AS node-base

ARG COMETBFT_VERSION=0.38.21
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

COPY --from=xian-configs . /opt/xian-configs
COPY --from=python-wheel-builder /tmp/wheels /tmp/wheels
COPY --from=cometbft-builder /out/cometbft /usr/local/bin/cometbft

RUN python -m pip install \
    /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels

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
