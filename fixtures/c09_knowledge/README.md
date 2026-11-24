# C09 demo knowledge corpus (synthetic)

**All documents here are invented demo data, not real Dublin City Council rules.**
They give the thin-slice scenario (two deliveries in Dublin 1/2, a van, a cargo bike,
an HGV) a small curated corpus with structured constraints.

- `*.md`: policy text; each heading with text becomes one passage (`sectionId`).
- `manifest.json`: publish metadata per document (applicability, validity, constraints).
  Entries with `"scenario": "conflict"` are only loaded on request; they contradict
  the HGV cordon so the `Conflict` path can be demonstrated.

Load into the local database (after `docker compose run --rm dbmate up`):

```sh
cd backend
uv run python -m app.components.c09_logistics_knowledge.demo_corpus            # base corpus
uv run python -m app.components.c09_logistics_knowledge.demo_corpus --conflict # + amendment
```
