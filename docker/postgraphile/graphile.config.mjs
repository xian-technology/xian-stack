import { PgAggregatesPreset } from "@graphile/pg-aggregates";
import { makeV4Preset } from "postgraphile/presets/v4";
import {
  PostGraphileConnectionFilterPreset,
} from "postgraphile-plugin-connection-filter";

const SIMPLE_COLLECTIONS = new Set(["omit", "only", "both"]);

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

function envSimpleCollections(name, defaultValue) {
  const rawValue = process.env[name] || defaultValue;
  if (!SIMPLE_COLLECTIONS.has(rawValue)) {
    throw new Error(`${name} must be one of: ${Array.from(SIMPLE_COLLECTIONS).join(", ")}`);
  }
  return rawValue;
}

const preset = {
  extends: [
    makeV4Preset({
      bodySizeLimit: envPositiveInteger("POSTGRAPHILE_BODY_SIZE_LIMIT_BYTES", 1048576),
      disableDefaultMutations: envFlag("POSTGRAPHILE_DISABLE_DEFAULT_MUTATIONS", true),
      graphiql: false,
      simpleCollections: envSimpleCollections("POSTGRAPHILE_SIMPLE_COLLECTIONS", "omit"),
    }),
    PgAggregatesPreset,
    PostGraphileConnectionFilterPreset,
  ],
};

export default preset;
