import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import yaml from "js-yaml";

const root = resolve("contracts/components");
const output = resolve(".tmp/contracts/openapi.yaml");
const componentDirectories = (await readdir(root, { withFileTypes: true }))
  .filter((entry) => entry.isDirectory())
  .map((entry) => entry.name)
  .sort();

if (componentDirectories.length === 0) {
  throw new Error("No component contract directories found.");
}

const aggregate = {
  openapi: "3.1.0",
  info: {
    title: "Intelligent Urban Logistics Platform API",
    version: "1.0.0",
    summary: "Aggregated component API contracts.",
    description:
      "Generated from the component-owned OpenAPI documents under contracts/components/.",
  },
  servers: [{ url: "/", description: "Relative URL for the deployed platform" }],
  tags: [],
  paths: {},
  components: {},
};

for (const directory of componentDirectories) {
  const file = join(root, directory, "openapi.yaml");
  const document = yaml.load(await readFile(file, "utf8"));

  if (!document || document.openapi !== "3.1.0") {
    throw new Error(`${file} must declare openapi: 3.1.0.`);
  }
  if (!document.info?.title || !document.info?.version) {
    throw new Error(`${file} must define info.title and info.version.`);
  }

  for (const tag of document.tags ?? []) {
    if (aggregate.tags.some((existing) => existing.name === tag.name)) {
      throw new Error(`Duplicate tag "${tag.name}" in ${file}.`);
    }
    aggregate.tags.push(tag);
  }

  for (const [path, methods] of Object.entries(document.paths ?? {})) {
    if (aggregate.paths[path]) {
      throw new Error(`Duplicate path "${path}" in ${file}.`);
    }
    aggregate.paths[path] = methods;
  }

  for (const [section, definitions] of Object.entries(document.components ?? {})) {
    aggregate.components[section] ??= {};
    for (const [name, definition] of Object.entries(definitions)) {
      if (aggregate.components[section][name]) {
        throw new Error(
          `Duplicate ${section} definition "${name}" in ${file}.`,
        );
      }
      aggregate.components[section][name] = definition;
    }
  }
}

await mkdir(resolve(".tmp/contracts"), { recursive: true });
await writeFile(output, yaml.dump(aggregate, { noRefs: true }), "utf8");
console.log(`Generated ${output} from ${componentDirectories.length} component(s).`);
