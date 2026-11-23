# Frontend (C01 Web Interface)

React single-page app for every thin-slice screen. Requires **Node 24 LTS**
(`.nvmrc`) and npm.

```sh
cd frontend
npm ci              # install the exact versions in package-lock.json
cp .env.example .env.local   # optional; defaults work for local development
npm run dev         # http://localhost:5173
```

The dev server forwards `/api/*` to the FastAPI backend at
`http://127.0.0.1:8000` (override with `API_PROXY_TARGET`).

## Sign in to Resources

Set `VITE_SUPABASE_URL` and `VITE_SUPABASE_PUBLISHABLE_KEY` in
`.env.local`, then restart Vite. Use the same Supabase development project
as the backend. Provision a synthetic manager and provider using the
[backend setup](../backend/README.md#api-contracts).

1. Open `/resources` and sign in with the synthetic account. The page loads
   the provider choices from C02. Multi-provider users and platform admins
   select an acting provider before viewing resources.
2. Choose a provider you manage, enter a unique identifier and name for a
   synthetic van, select a location, and register it. Check the success message
   and the new entry in that provider’s list.
3. Switch providers and check the list again. An operator can view their
   provider’s resources. Registration requires a manager or platform admin.
4. Sign out and confirm that the sign-in form replaces the list and draft.

The route tests exercise expired sessions and a server-side permission denial
with synthetic sessions. Run `npm test -- src/test/resources-page.test.tsx`.

## Checks

Run all of these before opening a PR:

```sh
npm run typecheck
npm run lint
npm run format:check
npm test
npm run build
```

## Stack

| Concern           | Library                                                              |
| ----------------- | -------------------------------------------------------------------- |
| Build, dev server | Vite 8, TypeScript (strict)                                          |
| UI                | React 19, Tailwind CSS v4, shadcn/ui on Base UI, Lucide icons        |
| Routing           | TanStack Router (file routes in `src/routes/`)                       |
| Server data       | TanStack Query (live views poll every 5 s)                           |
| API types         | `openapi-typescript` + `openapi-fetch`, generated from FastAPI       |
| Map               | MapLibre GL JS + `react-map-gl/maplibre`, OpenFreeMap tiles (no key) |
| Charts, forms     | Recharts, React Hook Form + Zod                                      |
| Tests, lint       | Vitest + Testing Library, ESLint, Prettier                           |

All versions are pinned exactly (`.npmrc` has `save-exact=true`). Pick releases
at least two weeks old when upgrading.

## Where things go

```text
src/
  routes/          one file per screen; the route tree is generated (routeTree.gen.ts)
  components/
    layout/        app shell, navigation, page header (C01)
    map/           shared Dublin map and its layers (C01)
    ui/            shadcn/ui components; add more with `npx shadcn@4.23.0 add <name>`
  features/        one folder per area, owned by its pair (create as needed)
  api/             typed backend client and generated schema
  lib/             config, query client, helpers
```

Each pair builds its screens in `src/features/<area>/` and renders them from the
matching route, which currently shows a placeholder naming the owner.

## Calling the backend

Once the FastAPI app is running, regenerate the API types:

```sh
npm run api:types   # reads http://127.0.0.1:8000/openapi.json (set API_SCHEMA_URL to change)
```

Then call it with full type checking:

```ts
import { api } from "@/api/client";

const { data, error } = await api.GET("/resources/{resource_id}", {
  params: { path: { resource_id } },
});
```

## Theme and colours

Colours are tokens in `src/index.css`, defined for light and dark mode. Use the
status tokens for conditions so the map and the UI agree: `bg-status-open`,
`bg-status-slow`, `bg-status-closed`, and `bg-quality-good|degraded|unknown`.

## Hosting

`npm run build` writes static files to `dist/`. `public/staticwebapp.config.json`
makes Azure Static Web Apps serve `index.html` for every route.

Map data © OpenStreetMap contributors; keep the attribution visible.
