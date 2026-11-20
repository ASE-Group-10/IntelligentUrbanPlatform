import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import Ajv2020 from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import yaml from "js-yaml";

const root = new URL("../", import.meta.url);
const load = (path) => yaml.load(readFileSync(new URL(path, root), "utf8"));
const fixture = (component, name) =>
  JSON.parse(readFileSync(new URL(`contracts/components/${component}/examples/${name}.json`, root), "utf8"));
const docs = ["c11-kpi-analytics", "c12-simulation-replay"].map((component) => {
  const doc = load(`contracts/components/${component}/openapi.yaml`);
  const ajv = new Ajv2020({ strict: false, allErrors: true });
  addFormats(ajv);
  // No custom x-* validator, coercion or mutation of invalid input is enabled.
  ajv.addSchema(doc, component);
  return { doc, validate: (name) => ajv.compile({ $ref: `${component}#/components/schemas/${name}` }) };
});
const [c11, c12] = docs;
const partial = fixture("c11-kpi-analytics", "kpi-set-partial");
const stale = fixture("c11-kpi-analytics", "kpi-set-stale");
const paused = fixture("c12-simulation-replay", "replay-frame-paused");
const breakdown = fixture("c11-kpi-analytics", "kpi-breakdown");

function check(validator, value, expected) {
  assert.equal(validator(value), expected, JSON.stringify(validator.errors));
}

test("all published HTTP request and response examples remain valid", () => {
  let count = 0;
  for (const { doc, validate } of docs) {
    for (const item of Object.values(doc.paths)) {
      for (const operation of Object.values(item)) {
        const contents = [operation.requestBody?.content];
        for (let response of Object.values(operation.responses)) {
          if (response.$ref) response = doc.components.responses[response.$ref.split("/").at(-1)];
          contents.push(response.content);
        }
        for (const content of contents.filter(Boolean)) {
          for (const media of Object.values(content)) {
            const schema = media.schema;
            const validator = schema.$ref
              ? validate(schema.$ref.split("/").at(-1))
              : (value) => schema.oneOf.filter((branch) => validate(branch.$ref.split("/").at(-1))(value)).length === 1;
            for (const example of Object.values(media.examples ?? {})) {
              check(validator, structuredClone(example.value), true);
              count++;
            }
          }
        }
      }
    }
  }
  assert.ok(count >= 53, "validate every current example and any future additions");
});

for (const [component, name] of [[c11, "C11Indicator"], [c12, "C12Indicator"]]) {
  const validate = component.validate(name);
  test(`${name}: unavailable data is null with a nonempty reason`, () => {
    const missing = structuredClone(partial.indicators[1]);
    check(validate, missing, true);
    check(validate, { ...missing, value: 0 }, false);
    const { reason, ...noReason } = missing;
    check(validate, noReason, false);
    check(validate, { ...missing, reason: "" }, false);
  });
  test(`${name}: measured zero works; available data needs value/time/observations`, () => {
    const measured = structuredClone(partial.indicators[0]);
    check(validate, { ...measured, value: 0 }, true);
    check(validate, { ...measured, value: null }, false);
    check(validate, { ...measured, lastUpdated: null }, false);
    check(validate, { ...measured, coverage: { eligible: 1, observed: 0 } }, false);
  });
  test(`${name}: an estimate needs a nonempty basis`, () => {
    const measured = structuredClone(partial.indicators[0]);
    check(validate, { ...measured, estimated: true }, false);
    check(validate, { ...measured, estimated: true, estimationBasis: "fixture-v1" }, true);
  });
}

test("stale sets require failure; non-stale sets cannot carry it", () => {
  const validate = c11.validate("C11KPISet");
  check(validate, stale, true);
  const { failure, ...noFailure } = stale;
  check(validate, noFailure, false);
  check(validate, { ...stale, failure: null }, false);
  check(validate, { ...partial, failure: "Refresh failed" }, false);
});

test("current sets contain only available indicators", () => {
  const validate = c11.validate("C11KPISet");
  check(validate, { ...partial, status: "current" }, false);
  check(validate, { ...partial, status: "current", indicators: [partial.indicators[0]] }, true);
});

test("interruptions require paused replay without changing recorded values", () => {
  const validate = c12.validate("C12ReplayFrame");
  check(validate, paused, true);
  for (const status of ["ready", "playing", "completed", "stopped"]) {
    check(validate, { ...paused, status }, false);
  }
  check(validate, { ...paused, status: "playing", interruption: null }, true);
});

test("breakdown accepts the cap and rejects oversized record/provenance arrays", () => {
  const validate = c11.validate("C11Breakdown");
  const records = Array.from({ length: 100 }, () => ({ synthetic: true }));
  const ids = Array.from({ length: 1000 }, (_, index) => `00000000-0000-4000-8000-${index.toString(16).padStart(12, "0")}`);
  check(validate, { ...breakdown, records, sourceEventIds: ids }, true);
  check(validate, { ...breakdown, records: [...records, {}] }, false);
  check(validate, { ...breakdown, sourceEventIds: [...ids, "11111111-1111-4111-8111-111111111111"] }, false);
  check(validate, { ...breakdown, sourceEventIds: [ids[0], ids[0]] }, false);
});

test("run scope and seek constraints remain machine-validated", () => {
  const scope = structuredClone(partial.scope);
  check(c11.validate("C11AnalysisScope"), { ...scope, scope: "live" }, false);
  const { runId, ...noRun } = scope;
  check(c11.validate("C11AnalysisScope"), noRun, false);
  const seek = fixture("c12-simulation-replay", "control-replay-seek");
  const { sessionId, ...command } = seek;
  check(c12.validate("C12ReplayControlCommand"), command, true);
  check(c12.validate("C12ReplayControlCommand"), { ...command, command: "play" }, false);
});
