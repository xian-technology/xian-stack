import { PgAggregatesPreset } from "@graphile/pg-aggregates";
import {
  PostGraphileAmberPreset,
} from "postgraphile/presets/amber";
import {
  PostGraphileConnectionFilterPreset,
} from "postgraphile-plugin-connection-filter";

const preset = {
  extends: [
    PostGraphileAmberPreset,
    PgAggregatesPreset,
    PostGraphileConnectionFilterPreset,
  ],
};

export default preset;
