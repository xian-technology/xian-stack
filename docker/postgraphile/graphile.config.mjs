import { PgAggregatesPreset } from "@graphile/pg-aggregates";
import { PostGraphileAmberPreset } from "postgraphile/presets/amber";
import {
  PostGraphileConnectionFilterPreset,
} from "postgraphile-plugin-connection-filter";

const COLLECTION_BEHAVIORS = {
  omit: "connection -list",
  only: "-connection list",
  both: "connection list",
};

function envFlag(name, defaultValue) {
  const rawValue = process.env[name];
  if (rawValue == null || rawValue === "") {
    return defaultValue;
  }
  return !["0", "false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"].includes(
    rawValue,
  );
}

function envPositiveInteger(name, defaultValue) {
  const rawValue = process.env[name];
  if (rawValue == null || rawValue === "") {
    return defaultValue;
  }
  const parsed = Number.parseInt(rawValue, 10);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) {
    throw new Error(`${name} must be a positive integer`);
  }
  return parsed;
}

function envCollectionBehavior(name, defaultValue) {
  const rawValue = process.env[name] || defaultValue;
  if (!Object.hasOwn(COLLECTION_BEHAVIORS, rawValue)) {
    throw new Error(`${name} must be one of: ${Object.keys(COLLECTION_BEHAVIORS).join(", ")}`);
  }
  return COLLECTION_BEHAVIORS[rawValue];
}

const preset = {
  extends: [
    PostGraphileAmberPreset,
    PgAggregatesPreset,
    PostGraphileConnectionFilterPreset,
  ],
  schema: {
    defaultBehavior: [
      envFlag("POSTGRAPHILE_DISABLE_DEFAULT_MUTATIONS", true) ? "-insert -update -delete" : "",
      envCollectionBehavior("POSTGRAPHILE_SIMPLE_COLLECTIONS", "omit"),
    ].join(" "),
  },
  grafserv: {
    graphiql: true,
    graphiqlPath: "/graphiql",
    graphqlPath: "/graphql",
    maxRequestLength: envPositiveInteger("POSTGRAPHILE_BODY_SIZE_LIMIT_BYTES", 1048576),
  },
};

export default preset;
