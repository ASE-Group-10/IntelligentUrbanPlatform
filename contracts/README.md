# API contracts

Each component owns its contracts in its own folder:

```text
contracts/
└── components/
    └── <component>/
        ├── openapi.yaml    # HTTP API (optional)
        ├── events.yaml     # event bus messages (optional)
        ├── internal.md     # internal interfaces (optional)
        └── examples/       # JSON examples (optional)
            ├── request-replan.json
            ├── replan-saved.json
            └── replan-conflict.json
```

Use one folder per component, named `c<NN>-<component-name>`, for example
`c03-resource-management` or `c07-disruption-detection`.

## Which files a component needs

The architecture uses three kinds of connection, and each one has its own file.
**Every file is optional.** Add only the ones that apply to your component.

| File | Use it for | Example |
| --- | --- | --- |
| `openapi.yaml` | HTTP operations: commands and queries from the browser (C01) or external clients | C03 resource registration |
| `events.yaml` | Messages published to or consumed from the event bus (RabbitMQ) | C06 publishes `CityStateUpdated` |
| `internal.md` | Calls from one component to another inside the same backend process, with no HTTP in between | C07 calls C05 `requestReplan` |
| `examples/` | JSON examples for any of the above | `request-replan.json` |

Some components have only events and internal interfaces (for example C07),
and others may have only an HTTP API. A component folder can be added as soon
as one of these files exists.

C01 Web Interface has no folder: it provides no operations, events or
internal interfaces of its own. It calls the HTTP APIs of the other
components, so each screen follows the `openapi.yaml` of the component it
uses. Sign-in, the bearer token and the acting provider are defined by C02
(`c02-identity-access/`).

## OpenAPI rules

- Use OpenAPI `3.1.0`.
- Keep at most one `openapi.yaml` per component.
- Tag every operation with the owning component, for example
  `C03 Resource Management`.
- Define paths and schemas in the component file; do not create a committed
  aggregate file.
- Include realistic request and response examples where useful.
- Use clear, stable names for paths, schemas, fields, and versions.
- Keep breaking changes coordinated with the affected producer and consumers.

The CI workflow automatically discovers all component OpenAPI files, generates a
temporary aggregate, validates it, and builds the Redoc site. Component folders
without an `openapi.yaml` are skipped. The generated aggregate is not source
code and must not be committed.

## Local checks

From the repository root:

```sh
npm ci
npm run contracts:lint
npm run contracts:build
cd dist && python3 -m http.server 8080
```

Open <http://localhost:8080/openapi/> to view the generated documentation.

## Event rules

Event contracts belong in one `events.yaml` per component:

- Use AsyncAPI `3.0.0`.
- Name messages after their event types.
- Define the channel or exchange, routing key, producer, and payload schema.
- Include a unique event ID, schema version, correlation ID, entity ID, and
  entity version where applicable.
- Include realistic message examples.
- Consumers must deduplicate messages by event ID.
- Breaking payload changes require a schema-version change and review by
  affected consumers.
- The producer defines the message. A consumer lists the messages it receives
  with `action: receive` and refers to the producer's definition rather than
  copying it.

Event files are currently source-only. The CI workflow does not yet validate or
render them.

## Internal interface rules

The architecture describes component APIs as "logical contracts, not separate
public HTTP endpoints for every function", and allows logical components to
share a process and database transaction. Those direct calls between components
are documented in `internal.md`:

- List the interfaces the component **provides** (other components call them)
  and the ones it **requires** (it calls other components).
- For each provided interface, give the signature, request and response fields
  (name, type, required, meaning), the `Result` failure kinds, and any
  idempotency or ordering rule.
- A required interface only names the operation, the providing component and
  how this component uses it. The provider's `internal.md` is the definition.
- Link the matching files in `examples/`.
- Name the typed model in the code that implements the contract, if there is
  one.
- Methods used only inside the component (for example C07 `classifyImpact`)
  are not contracts and do not belong here.

## Examples

- Put examples in the `examples/` folder of the component that owns the
  contract, so the producer of an event or the provider of an operation.
- Use kebab-case names that say what the example is: `request-replan.json`,
  `replan-saved.json`, `city-state-updated-event.json`.
- Use synthetic data only. Keep shared IDs consistent across examples, so a
  chain (closure → disruption → replan) can be followed.
- Backend tests may load these files as fixtures. If you rename or change one,
  run `cd backend && uv run pytest`.
