import { existsSync } from "node:fs";
import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import yaml from "js-yaml";

const root = resolve("contracts/components");
const output = resolve(".tmp/contracts/asyncapi.yaml");
const componentDirectories = (await readdir(root, { withFileTypes: true }))
  .filter((entry) => entry.isDirectory())
  .map((entry) => entry.name)
  .filter((name) => existsSync(join(root, name, "events.yaml")))
  .sort();

if (componentDirectories.length === 0) {
  throw new Error("No component events.yaml documents found.");
}

const aggregate = {
  asyncapi: "3.0.0",
  info: {
    title: "Intelligent Urban Logistics Platform Events",
    version: "1.0.0",
    description:
      "Generated from the component-owned AsyncAPI documents under contracts/components/.",
  },
  defaultContentType: "application/json",
  servers: {},
  channels: {},
  operations: {},
  components: {},
};

function mergeNamedCollection(target, source, sourceName, section) {
  for (const [name, value] of Object.entries(source ?? {})) {
    if (target[name]) {
      if (section === "server") {
        continue;
      }
      if (JSON.stringify(target[name]) !== JSON.stringify(value)) {
        throw new Error(`Conflicting ${section} "${name}" in ${sourceName}`);
      }
      continue;
    }
    target[name] = value;
  }
}

function rewriteReferences(value, componentDirectory) {
  if (Array.isArray(value)) {
    return value.map((child) => rewriteReferences(child, componentDirectory));
  }
  if (!value || typeof value !== "object") {
    return value;
  }

  for (const [key, child] of Object.entries(value)) {
    if (key === "$ref" && typeof child === "string") {
      const externalMatch = child.match(
        /^\.\.\/([^/]+)\/events\.yaml#\/(components|channels|operations)\/(.+)$/,
      );
      if (externalMatch) {
        const [, targetDirectory, section, rest] = externalMatch;
        const parts = rest.split("/");
        const name = parts.pop();
        value[key] =
          parts.length > 0
            ? `#/${section}/${parts.join("/")}/${targetDirectory}-${name}`
            : `#/${section}/${targetDirectory}-${name}`;
        continue;
      }

      const internalMatch = child.match(
        /^#\/(components|channels|operations)\/(.+)$/,
      );
      if (internalMatch) {
        const [, section, rest] = internalMatch;
        const parts = rest.split("/");
        if (section === "channels") {
          parts[0] = `${componentDirectory}-${parts[0]}`;
          value[key] = `#/${section}/${parts.join("/")}`;
        } else if (section === "operations") {
          value[key] = `#/${section}/${componentDirectory}-${rest}`;
        } else {
          const name = parts.pop();
          value[key] =
            parts.length > 0
              ? `#/${section}/${parts.join("/")}/${componentDirectory}-${name}`
              : `#/${section}/${componentDirectory}-${name}`;
        }
      }
    } else {
      value[key] = rewriteReferences(child, componentDirectory);
    }
  }
  return value;
}

function prefixKeys(values, componentDirectory) {
  return Object.fromEntries(
    Object.entries(values ?? {}).map(([name, value]) => [
      `${componentDirectory}-${name}`,
      value,
    ]),
  );
}

for (const directory of componentDirectories) {
  const file = join(root, directory, "events.yaml");
  const document = rewriteReferences(
    yaml.load(await readFile(file, "utf8")),
    directory,
  );

  if (!document || document.asyncapi !== "3.0.0") {
    throw new Error(`${file} must declare asyncapi: 3.0.0.`);
  }
  if (!document.info?.title || !document.info?.version) {
    throw new Error(`${file} must define info.title and info.version.`);
  }

  mergeNamedCollection(
    aggregate.servers,
    prefixKeys(document.servers, directory),
    file,
    "server",
  );
  mergeNamedCollection(
    aggregate.channels,
    prefixKeys(document.channels, directory),
    file,
    "channel",
  );
  mergeNamedCollection(
    aggregate.operations,
    prefixKeys(document.operations, directory),
    file,
    "operation",
  );

  for (const [section, definitions] of Object.entries(document.components ?? {})) {
    aggregate.components[section] ??= {};
    mergeNamedCollection(
      aggregate.components[section],
      prefixKeys(definitions, directory),
      file,
      `${section} definition`,
    );
  }
}

await mkdir(resolve(".tmp/contracts"), { recursive: true });
await writeFile(output, yaml.dump(aggregate, { noRefs: true }), "utf8");
console.log(`Generated ${output} from ${componentDirectories.length} component(s).`);
