FROM node:24-bullseye

WORKDIR /usr/src/app

RUN npm install -g postgraphile postgraphile-plugin-connection-filter @graphile/pg-aggregates

EXPOSE 5000
