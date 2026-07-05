import pg from "pg";

const DEFAULT_REQUIRED_TABLES = "blocks,transactions,events,addresses,shielded_outputs";
const DEFAULT_TIMEOUT_SECONDS = 60;
const POLL_INTERVAL_MS = 1000;

function readIntegerEnv(name, defaultValue) {
  const rawValue = process.env[name] ?? String(defaultValue);
  if (!/^[0-9]+$/.test(rawValue)) {
    console.error(`${name} must be a non-negative integer`);
    process.exit(1);
  }
  return Number(rawValue);
}

function requiredTables() {
  const rawValue = process.env.POSTGRAPHILE_REQUIRED_TABLES ?? DEFAULT_REQUIRED_TABLES;
  return rawValue
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function tableNames(connectionString, schema, tables) {
  const client = new pg.Client({
    application_name: "xian-postgraphile-schema-wait",
    connectionString,
    connectionTimeoutMillis: 5000,
  });
  try {
    await client.connect();
    const result = await client.query(
      `
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = $1
          AND table_type = 'BASE TABLE'
          AND table_name = ANY($2::text[])
      `,
      [schema, tables],
    );
    return new Set(result.rows.map((row) => row.table_name));
  } finally {
    await client.end().catch(() => {});
  }
}

async function main() {
  const connectionString = process.env.POSTGRAPHILE_CONNECTION;
  if (!connectionString) {
    console.error("POSTGRAPHILE_CONNECTION must be set");
    process.exit(1);
  }

  const schema = process.env.POSTGRAPHILE_SCHEMA || "public";
  const timeoutSeconds = readIntegerEnv(
    "POSTGRAPHILE_SCHEMA_WAIT_TIMEOUT_SECONDS",
    DEFAULT_TIMEOUT_SECONDS,
  );
  const tables = requiredTables();
  if (timeoutSeconds === 0 || tables.length === 0) {
    return;
  }

  const deadline = Date.now() + timeoutSeconds * 1000;
  let lastLogAt = 0;
  let lastMissing = tables;
  let lastError = null;

  while (Date.now() <= deadline) {
    try {
      const foundTables = await tableNames(connectionString, schema, tables);
      lastMissing = tables.filter((table) => !foundTables.has(table));
      lastError = null;
      if (lastMissing.length === 0) {
        console.error(`BDS schema ready for PostGraphile: ${tables.join(", ")}`);
        return;
      }
    } catch (error) {
      lastError = error;
    }

    const now = Date.now();
    if (now - lastLogAt >= 5000) {
      if (lastError) {
        console.error(`waiting for BDS schema: ${lastError.message}`);
      } else {
        console.error(`waiting for BDS schema tables: ${lastMissing.join(", ")}`);
      }
      lastLogAt = now;
    }
    await sleep(Math.min(POLL_INTERVAL_MS, Math.max(0, deadline - now)));
  }

  if (lastError) {
    console.error(
      `timed out waiting for BDS schema after ${timeoutSeconds}s: ${lastError.message}`,
    );
  } else {
    console.error(
      `timed out waiting for BDS schema tables after ${timeoutSeconds}s: ${lastMissing.join(", ")}`,
    );
  }
  process.exit(1);
}

await main();
