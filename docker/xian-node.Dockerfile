# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.14-bookworm@sha256:4564a2be4617886b3a7ed704c91e31b95efd1f42d5941f5bb5e53efb151edaa3
ARG GO_IMAGE=golang:1.25-bookworm@sha256:29e59af995c51a5bf63d072eca973b918e0e7af4db0e4667aa73f1b8da1a6d8c

FROM ${GO_IMAGE} AS cometbft-builder

ARG COMETBFT_VERSION=0.39.1
ARG COMETBFT_SOURCE_URL=https://github.com/cometbft/cometbft/archive/refs/tags/v0.39.1.tar.gz
ARG COMETBFT_SOURCE_SHA256=3349c89ed0c7d076b9fd5bfd88432481c68c69d43593371633ca0fe51327650b
ARG TARGETOS=linux
ARG TARGETARCH

ENV CGO_ENABLED=0

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    tar \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/cometbft-src

RUN wget -O /tmp/cometbft.tar.gz "${COMETBFT_SOURCE_URL}" \
    && echo "${COMETBFT_SOURCE_SHA256}  /tmp/cometbft.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/cometbft.tar.gz --strip-components=1 -C /tmp/cometbft-src \
    && rm -f /tmp/cometbft.tar.gz

RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    GOOS="${TARGETOS}" GOARCH="${TARGETARCH}" GOBIN=/out \
    go build -trimpath -buildvcs=false -ldflags="-s -w" \
    -o /out/cometbft ./cmd/cometbft

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
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-contracting/packages/xian-accounts \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-contracting/packages/xian-runtime-types \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-contracting/packages/xian-native-tracer \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-contracting/packages/xian-vm-core \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-contracting/packages/xian-zk \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-py \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-contracting \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-abci

FROM ${PYTHON_IMAGE} AS node-base

ARG COMETBFT_VERSION=0.39.1
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
ARG S6_OVERLAY_NOARCH_SHA256=42e038a9a00fc0fef70bf0bc42f625a9c14f8ecdfe77d4ad93281edf717e10c5
ARG S6_OVERLAY_X86_64_SHA256=8bcbc2cada58426f976b159dcc4e06cbb1454d5f39252b3bb0c778ccf71c9435
ARG S6_OVERLAY_AARCH64_SHA256=c8fd6b1f0380d399422fc986a1e6799f6a287e2cfa24813ad0b6a4fb4fa755cc
ARG TARGETARCH

RUN case "${TARGETARCH}" in \
        amd64) S6_ARCH="x86_64"; S6_ARCH_SHA256="${S6_OVERLAY_X86_64_SHA256}" ;; \
        arm64) S6_ARCH="aarch64"; S6_ARCH_SHA256="${S6_OVERLAY_AARCH64_SHA256}" ;; \
        *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && wget -O /tmp/s6-overlay-noarch.tar.xz \
        "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz" \
    && wget -O /tmp/s6-overlay-${S6_ARCH}.tar.xz \
        "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${S6_ARCH}.tar.xz" \
    && echo "${S6_OVERLAY_NOARCH_SHA256}  /tmp/s6-overlay-noarch.tar.xz" | sha256sum -c - \
    && echo "${S6_ARCH_SHA256}  /tmp/s6-overlay-${S6_ARCH}.tar.xz" | sha256sum -c - \
    && tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz \
    && tar -C / -Jxpf /tmp/s6-overlay-${S6_ARCH}.tar.xz \
    && rm -f \
        /tmp/s6-overlay-noarch.tar.xz \
        /tmp/s6-overlay-${S6_ARCH}.tar.xz

COPY docker/s6-overlay/ /

WORKDIR /opt/xian
ENTRYPOINT ["/init"]

FROM split AS dev

RUN python -m pip install pytest parameterized

WORKDIR /workspace
CMD ["/bin/bash"]
