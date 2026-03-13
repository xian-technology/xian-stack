FROM node:24-bullseye

WORKDIR /usr/src/app

COPY docker/postgraphile/package.json ./package.json
COPY docker/postgraphile/package-lock.json ./package-lock.json
COPY docker/postgraphile/graphile.config.mjs ./graphile.config.mjs
COPY docker/postgraphile/start-postgraphile.sh ./start-postgraphile.sh

RUN npm ci --omit=dev
RUN chmod +x ./start-postgraphile.sh

ENV PATH="/usr/src/app/node_modules/.bin:${PATH}"

EXPOSE 5000
