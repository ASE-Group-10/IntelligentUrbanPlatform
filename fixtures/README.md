# Shared demo data

`dublin-scenario.json` is the one place the thin-slice demo takes its run,
times and IDs from, so the route (C05), the closure (C06/C07) and the map (C01)
all use the same values. Agreed in #42.

| Field | Meaning |
| --- | --- |
| `runId` | Simulation run the demo uses (all events carry it) |
| `seed` | Seed for anything random in the run |
| `startTime` | Demo start time (UTC) |
| `correlationId` | Shared trace for the demo delivery and the closure that revises its plan |
| `closure` | The road closure the feed script injects into C06 |
| `segments` | Demo road segments with GeoJSON geometry (`[lon, lat]`) for the C01 map and C05 routes (#43) |

## Segments

The segment keys come from the contract examples, but those OpenStreetMap way
IDs no longer exist, so each key is a stand-in for a real street. Its geometry
is copied from the current OSM ways listed in `osmWayIds`:

| Key | Street | Role |
| --- | --- | --- |
| `seg:4471023` | Eden Quay (eastbound) | Closed by the demo feed |
| `seg:4471024` | Abbey Street Lower (eastbound) | Detour in C05's revised plan |

C05 can route on these keys and C01 draws them. To add a segment, keep the
same shape and add its key to any route that uses it.

Map data © OpenStreetMap contributors, available under the
[ODbL](https://www.openstreetmap.org/copyright).

The IDs match the contract examples in `contracts/components/c06-city-monitoring/examples/`.
Inject the closure with `python -m app.demo.feed` (see `backend/README.md`).
