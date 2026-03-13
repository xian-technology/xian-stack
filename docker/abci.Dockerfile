FROM node:24-bullseye AS node-runtime

FROM python:3.11.9-bullseye

RUN apt-get update && apt-get install -y \
    curl \
    git \
    libhdf5-dev \
    wget

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/include/node /usr/local/include/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

WORKDIR /usr/src/app

RUN ARCH=$(uname -m); \
    case ${ARCH} in \
        x86_64) COMETBFT_ARCH="amd64" ;; \
        aarch64) COMETBFT_ARCH="arm64" ;; \
        *) echo "Unsupported architecture: ${ARCH}" && exit 1 ;; \
    esac && \
    wget https://github.com/cometbft/cometbft/releases/download/v0.38.12/cometbft_0.38.12_linux_${COMETBFT_ARCH}.tar.gz \
    && tar -xf cometbft_0.38.12_linux_${COMETBFT_ARCH}.tar.gz \
    && rm cometbft_0.38.12_linux_${COMETBFT_ARCH}.tar.gz \
    && ./cometbft init

RUN npm install pm2 -g

RUN pm2 install pm2-logrotate \
    && pm2 set pm2-logrotate:max_size 100M \
    && pm2 set pm2-logrotate:retain 7

EXPOSE 26657
EXPOSE 26656
EXPOSE 26660

CMD ["tail", "-f", "/dev/null"]
