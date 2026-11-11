import { fileURLToPath } from "node:url";

export function connectionString(environment) {
  const {
    PGPORT: port = "5432",
    PGUSER: rawUser = "xian_graphql",
    PGPASSWORD: rawPassword = "",
    PGDATABASE: rawDatabase = "xian",
  } = environment;
  if (!/^[0-9]+$/.test(port) || Number(port) <= 0 || Number(port) > 65535) {
    throw new Error("PGPORT must be a port between 1 and 65535");
  }
  const user = encodeURIComponent(rawUser);
  const password = encodeURIComponent(rawPassword);
  const database = encodeURIComponent(rawDatabase);
  return `postgres://${user}:${password}@postgres:${port}/${database}`;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  try {
    process.stdout.write(connectionString(process.env));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
