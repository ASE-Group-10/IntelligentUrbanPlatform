// Generates src/api/schema.d.ts from the FastAPI OpenAPI schema.
// Plain Node so it behaves the same on macOS, Linux and Windows.
// Usage: npm run api:types   (override the source with API_SCHEMA_URL)
import { writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

import openapiTS, { astToString } from "openapi-typescript";

const source = process.env.API_SCHEMA_URL ?? "http://127.0.0.1:8000/openapi.json";
const target = new URL("../src/api/schema.d.ts", import.meta.url);

try {
  const ast = await openapiTS(new URL(source));
  const header = `/** Generated from ${source} by \`npm run api:types\`. Do not edit by hand. */\n\n`;
  await writeFile(target, header + astToString(ast));
  console.log(`Wrote ${fileURLToPath(target)} from ${source}`);
} catch (error) {
  console.error(`Could not generate API types from ${source}:`, error.message);
  console.error("Is the backend running? Set API_SCHEMA_URL to use another address.");
  process.exitCode = 1;
}
