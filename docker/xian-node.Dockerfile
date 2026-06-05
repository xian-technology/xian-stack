# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.14-bookworm@sha256:4564a2be4617886b3a7ed704c91e31b95efd1f42d5941f5bb5e53efb151edaa3
ARG GO_IMAGE=golang:1.25-bookworm@sha256:29e59af995c51a5bf63d072eca973b918e0e7af4db0e4667aa73f1b8da1a6d8c
ARG RUST_IMAGE=rust:1.95-bookworm@sha256:225aa827d55fae9816a0492284592827e794a5247c6c6a961c3b471b344295ec
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.6.11@sha256:fb91e82e8643382d5bce074ba0d167677d678faff4bd518dac670476d19b159c
ARG DEBIAN_SNAPSHOT=20260515T000000Z
ARG SOURCE_DATE_EPOCH=1704067200
ARG PIP_VERSION=26.0.1
ARG PACKAGING_VERSION=26.2
ARG WHEEL_VERSION=0.46.3
ARG MATURIN_VERSION=1.13.1

FROM ${GO_IMAGE} AS cometbft-builder

ARG DEBIAN_SNAPSHOT
ARG COMETBFT_VERSION=0.39.3
ARG COMETBFT_SOURCE_URL=https://github.com/cometbft/cometbft/archive/refs/tags/v0.39.3.tar.gz
ARG COMETBFT_SOURCE_SHA256=f674a39ce3bdb3f62ac547a1c3296142ef1433f56fff185055c50a38b3afeee8
ARG TARGETOS=linux
ARG TARGETARCH

ENV CGO_ENABLED=0

RUN set -eux; \
    printf 'Acquire::Check-Valid-Until "false";\nAcquire::Retries "3";\n' > /etc/apt/apt.conf.d/99snapshot; \
    printf 'Types: deb\nURIs: http://snapshot.debian.org/archive/debian/%s/\nSuites: bookworm bookworm-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\nTypes: deb\nURIs: http://snapshot.debian.org/archive/debian-security/%s/\nSuites: bookworm-security\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n' "${DEBIAN_SNAPSHOT}" "${DEBIAN_SNAPSHOT}" > /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
    ca-certificates \
    tar \
    wget; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/cometbft-src

RUN wget -O /tmp/cometbft.tar.gz "${COMETBFT_SOURCE_URL}" \
    && echo "${COMETBFT_SOURCE_SHA256}  /tmp/cometbft.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/cometbft.tar.gz --strip-components=1 -C /tmp/cometbft-src \
    && sed -i "s/TMCoreSemVer = \"[^\"]*\"/TMCoreSemVer = \"${COMETBFT_VERSION}\"/" /tmp/cometbft-src/version/version.go \
    && grep -F "TMCoreSemVer = \"${COMETBFT_VERSION}\"" /tmp/cometbft-src/version/version.go \
    && rm -f /tmp/cometbft.tar.gz

RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    GOOS="${TARGETOS}" GOARCH="${TARGETARCH}" GOBIN=/out \
    go build -trimpath -buildvcs=false -ldflags="-s -w -buildid=" \
    -o /out/cometbft ./cmd/cometbft

FROM ${RUST_IMAGE} AS rust-toolchain

FROM ${UV_IMAGE} AS uv-bin

FROM ${PYTHON_IMAGE} AS python-wheel-builder

ARG DEBIAN_SNAPSHOT
ARG SOURCE_DATE_EPOCH
ARG PIP_VERSION
ARG PACKAGING_VERSION
ARG WHEEL_VERSION
ARG MATURIN_VERSION

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONHASHSEED=0 \
    SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}

RUN set -eux; \
    printf 'Acquire::Check-Valid-Until "false";\nAcquire::Retries "3";\n' > /etc/apt/apt.conf.d/99snapshot; \
    printf 'Types: deb\nURIs: http://snapshot.debian.org/archive/debian/%s/\nSuites: bookworm bookworm-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\nTypes: deb\nURIs: http://snapshot.debian.org/archive/debian-security/%s/\nSuites: bookworm-security\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n' "${DEBIAN_SNAPSHOT}" "${DEBIAN_SNAPSHOT}" > /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
    build-essential; \
    rm -rf /var/lib/apt/lists/*

ENV CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    PATH=/usr/local/cargo/bin:${PATH}

WORKDIR /tmp/build

COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup
COPY --from=xian-py . /tmp/build/xian-py
COPY --from=xian-contracting . /tmp/build/xian-contracting
COPY --from=xian-abci . /tmp/build/xian-abci
COPY docker/python-runtime-requirements.txt /tmp/build/python-runtime-requirements.txt

RUN python -m pip install --upgrade \
    pip=="${PIP_VERSION}" \
    packaging=="${PACKAGING_VERSION}" \
    wheel=="${WHEEL_VERSION}" \
    maturin=="${MATURIN_VERSION}" \
    && python -m pip download --require-hashes --dest /tmp/wheels \
    -r /tmp/build/python-runtime-requirements.txt \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-contracting/packages/xian-accounts \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-contracting/packages/xian-compiler-core \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-contracting/packages/xian-runtime-types \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-contracting/packages/xian-fastpath-core \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-contracting/packages/xian-vm-core \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-contracting/packages/xian-zk \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-py \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-contracting \
    && python -m pip wheel --no-deps --wheel-dir /tmp/wheels /tmp/build/xian-abci

FROM ${PYTHON_IMAGE} AS node-base

ARG DEBIAN_SNAPSHOT
ARG COMETBFT_VERSION=0.39.3
ARG TARGETARCH
ARG SOURCE_DATE_EPOCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONHASHSEED=0 \
    SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH} \
    XIAN_CONFIGS_DIR=/opt/xian-configs

RUN set -eux; \
    printf 'Acquire::Check-Valid-Until "false";\nAcquire::Retries "3";\n' > /etc/apt/apt.conf.d/99snapshot; \
    printf 'Types: deb\nURIs: http://snapshot.debian.org/archive/debian/%s/\nSuites: bookworm bookworm-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\nTypes: deb\nURIs: http://snapshot.debian.org/archive/debian-security/%s/\nSuites: bookworm-security\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n' "${DEBIAN_SNAPSHOT}" "${DEBIAN_SNAPSHOT}" > /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    procps \
    wget \
    xz-utils; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/*; \
    rm -f /var/log/apt/* /var/log/dpkg.log /var/log/alternatives.log; \
    rm -f /var/cache/ldconfig/aux-cache

WORKDIR /opt/xian

COPY --from=uv-bin /uv /uvx /bin/

RUN mkdir -p /opt/xian-configs
COPY --from=xian-configs README.md /opt/xian-configs/README.md
COPY --from=xian-configs contracts /opt/xian-configs/contracts
COPY --from=xian-configs networks /opt/xian-configs/networks
COPY --from=xian-configs templates /opt/xian-configs/templates
COPY --from=xian-configs contract-templates /opt/xian-configs/contract-templates
COPY --from=python-wheel-builder /tmp/wheels /tmp/wheels
COPY --from=python-wheel-builder /tmp/build/python-runtime-requirements.txt /tmp/python-runtime-requirements.txt
COPY --from=cometbft-builder /out/cometbft /usr/local/bin/cometbft

# Release image reproducibility: uv injects two non-deterministic artifacts into
# this layer that `rewrite-timestamp` cannot normalize (they are bytes *inside*
# files, not filesystem mtimes), and they were the sole source of release rootfs
# drift:
#   1. its package cache under /root/.cache/uv (content-addressed, random names)
#   2. a per-package <dist-info>/uv_cache.json carrying a wall-clock install
#      timestamp (written for every --find-links install regardless of --no-cache
#      or --link-mode), whose hash also perturbs the package RECORD.
# --no-cache keeps (1) out of the image; the post-install cleanup removes (2) and
# strips its now-stale RECORD entries so the layer is byte-identical every build.
RUN uv pip install --system --no-cache --no-index --find-links /tmp/wheels --require-hashes \
    -r /tmp/python-runtime-requirements.txt \
    && uv pip install --system --no-cache --no-index --find-links /tmp/wheels --no-deps \
    /tmp/wheels/xian_tech_accounts-*.whl \
    /tmp/wheels/xian_tech_compiler_core-*.whl \
    /tmp/wheels/xian_tech_runtime_types-*.whl \
    /tmp/wheels/xian_tech_fastpath_core-*.whl \
    /tmp/wheels/xian_tech_vm_core-*.whl \
    /tmp/wheels/xian_tech_zk-*.whl \
    /tmp/wheels/xian_tech_py-*.whl \
    /tmp/wheels/xian_tech_contracting-*.whl \
    /tmp/wheels/xian_tech_abci-*.whl \
    && rm -rf /tmp/wheels /tmp/python-runtime-requirements.txt /root/.cache/uv \
    && find /usr/local/lib -name uv_cache.json -delete \
    && find /usr/local/lib -name RECORD -exec sed -i '/uv_cache\.json,/d' {} +

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
    && wget --no-hsts -O /tmp/s6-overlay-noarch.tar.xz \
        "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz" \
    && wget --no-hsts -O /tmp/s6-overlay-${S6_ARCH}.tar.xz \
        "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${S6_ARCH}.tar.xz" \
    && echo "${S6_OVERLAY_NOARCH_SHA256}  /tmp/s6-overlay-noarch.tar.xz" | sha256sum -c - \
    && echo "${S6_ARCH_SHA256}  /tmp/s6-overlay-${S6_ARCH}.tar.xz" | sha256sum -c - \
    && tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz \
    && tar -C / -Jxpf /tmp/s6-overlay-${S6_ARCH}.tar.xz \
    && rm -f \
        /root/.wget-hsts \
        /tmp/s6-overlay-noarch.tar.xz \
        /tmp/s6-overlay-${S6_ARCH}.tar.xz

COPY docker/s6-overlay/ /

WORKDIR /opt/xian
ENTRYPOINT ["/init"]

FROM split AS dev

RUN uv pip install --system pytest parameterized

WORKDIR /workspace
CMD ["/bin/bash"]
