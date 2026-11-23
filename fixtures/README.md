# Shared demo data

`dublin-scenario.json` is the one place the thin-slice demo takes its run,
times and IDs from, so the route (C05), the closure (C06/C07) and the map (C01)
all use the same values. Agreed in #42.

| Field | Meaning |
| --- | --- |
| `runId` | Simulation run the demo uses (all events carry it) |
| `seed` | Seed for anything random in the run |
| `startTime` | Demo start time (UTC) |
| `correlationId` | Trace ID of the scripted closure, to follow it through the logs |
| `closure` | The road closure the feed script injects into C06 |
| `segments` | Demo road segments with geometry, filled in by C05 (#43). Empty until then |

The IDs match the contract examples in `contracts/components/c06-city-monitoring/examples/`.
Inject the closure with `python -m app.demo.feed` (see `backend/README.md`).
