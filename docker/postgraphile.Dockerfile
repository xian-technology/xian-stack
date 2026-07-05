FROM node:24-alpine

WORKDIR /usr/src/app

COPY docker/postgraphile/package.json ./package.json
COPY docker/postgraphile/package-lock.json ./package-lock.json
COPY docker/postgraphile/graphile.config.mjs ./graphile.config.mjs
COPY docker/postgraphile/start-postgraphile.sh ./start-postgraphile.sh
COPY docker/postgraphile/wait-for-bds-schema.mjs ./wait-for-bds-schema.mjs

RUN npm ci --omit=dev
RUN chmod +x ./start-postgraphile.sh

ENV PATH="/usr/src/app/node_modules/.bin:${PATH}"

EXPOSE 5000
