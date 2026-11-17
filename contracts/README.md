# API contracts

Each component owns its HTTP contract:

```text
contracts/
└── components/
    └── <component>/
        ├── openapi.yaml
        └── events.yaml
```

## OpenAPI rules

- Use OpenAPI `3.1.0`.
- Keep one `openapi.yaml` per component.
- Tag every operation with the owning component, for example
  `C03 Resource Management`.
- Define paths and schemas in the component file; do not create a committed
  aggregate file.
- Include realistic request and response examples where useful.
- Use clear, stable names for paths, schemas, fields, and versions.
- Keep breaking changes coordinated with the affected producer and consumers.

The CI workflow automatically discovers all component OpenAPI files, generates a
temporary aggregate, validates it, and builds the Redoc site. The generated
aggregate is not source code and must not be committed.

## Local checks

From the repository root:

```sh
npm ci
npm run contracts:lint
npm run contracts:build
cd dist && python3 -m http.server 8080
```

Open <http://localhost:8080/openapi/> to view the generated documentation.

Event contracts belong in `events.yaml`. Event rendering is not part of the
current OpenAPI/Redoc workflow.
