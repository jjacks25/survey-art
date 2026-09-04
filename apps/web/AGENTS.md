# apps/web/ — React + Mantine SPA

The frontend: a single-page app where a teammate submits an address, watches the job
run, and downloads the resulting documents. Built with **React + Mantine**, bundled by
Vite. In AWS it is a static build served from S3 via CloudFront; locally it runs the
Vite dev server in a container.

## Runtime config (important)

The app reads **`/config.json` at runtime** (`src/config.ts`) so one build works in any
environment — nothing account-specific is baked into the bundle.

- **Local dev:** `public/config.json` ships `authDisabled: true` and `apiBase:
  http://localhost:8000` (the api container).
- **AWS:** `make deploy web` (via `infra/deploy.py --web`) writes `config.json` into the S3
  bucket from stack outputs — `apiBase: ""` (same origin, CloudFront proxies `/api/*`),
  `authDisabled: false`, and the Cognito authority/clientId/domain.

## Auth flow

When auth is enabled, `main.tsx` wraps the app in `react-oidc-context` (Authorization
Code + PKCE against the Cognito user pool). The access token is attached as a Bearer
header by `src/api.ts`; API Gateway's JWT authorizer validates it. When `authDisabled`,
the app renders directly with no token.

## UI components

Before building or changing any UI, check the **Mantine docs** for an existing
component/pattern that fits — https://mantine.dev/core/package (component API) and
https://ui.mantine.dev (ready-made layouts) — rather than hand-rolling markup or
reaching for another library. Mantine (`@mantine/core`) is already the project's
component library; use its primitives (`Tabs`, `Select`, `TextInput`, `Table`, `Alert`,
`Badge`, etc.) instead of raw HTML elements or custom CSS where a Mantine component
covers the case.

## Files

- `src/main.tsx` — loads config, mounts with/without the Cognito provider.
- `src/App.tsx` — sign-in gate + the dashboard (address form, status polling, results tabs).
- `src/api.ts` — typed API client (Bearer token when present).
- `src/config.ts` — runtime config loader.

## Search mode: Account/Parcel # is the default

The `mode` tab defaults to `"account"` and is listed before `"address"` — most users
of this tool already have the parcel/account number in hand (that's the whole point of
the county SOPs), so it's the faster path. If you add a third search mode, put it after
these two rather than reordering existing tabs out from under muscle memory.

## Search history: permanent left-hand panel

A fixed, always-visible left panel (a `Box` in a full-height flex `Group`, not an
`AppShell.Navbar`/`Drawer` — there's no toggle, it never closes) lists past searches,
backed by `GET /api/jobs` (`api.listJobs()` → `JobSummary[]`, most-recent-first — see
[`apps/api/AGENTS.md`](../../apps/api/AGENTS.md)). Loaded once on mount, plus after
`submit()` creates a job and whenever `poll()` observes a job hit a terminal status —
there's no auto-polling of the list itself, just a manual refresh `ActionIcon` and those
two event-driven refreshes.

**One entry per property, not per job.** `propertyHistory` (a `useMemo` over `history`)
dedupes by `docPrefix` (falling back to `address` for jobs with no `doc_prefix` yet —
still running, or failed before upload) and keeps only the first occurrence of each key.
Since `history` is already most-recent-first, "first occurrence" is "most recent search
for that property" — repeated searches for the same parcel collapse to one row that
always reflects the latest job, instead of piling up duplicate entries. If you add a new
grouping key, make sure it's still something `worker.py`'s `_doc_prefix()` computes
consistently for the same property across searches (see
[`apps/worker/survey_art/AGENTS.md`](../worker/survey_art/AGENTS.md)) — a key that isn't stable
per-property will silently stop deduping.

Clicking an entry calls `selectJob(jobId)`, which reuses the exact same `poll()`/
`pollRef` machinery as a fresh `submit()` — an in-progress job picked from history keeps
live-updating exactly like one just submitted, and a completed one immediately shows its
files via `poll()`'s own COMPLETED branch. The list itself is a lightweight `JobSummary`
(no `logs`/`metadata`) — safe to fetch on every refresh.

## Job results: four tabs

Once a job exists, `App.tsx` polls `GET /api/jobs/{id}` every **1.5s** (this is the
whole "real-time" mechanism — there's no websocket/SSE; see
[`docs/architecture.md`](../../docs/architecture.md#job-lifecycle) for why) and renders
four `Tabs.Panel`s from the response:

- **Logs** — `job.logs` (a running `string[]`) in a `<Code block>`. Guard
  `!job.logs || job.logs.length === 0` before rendering — an omitted/undefined `logs`
  field previously caused a full white-screen crash (`job.logs.length` on `undefined`)
  when a stale container returned an old schema; keep the guard even though the schema
  is now stable, since it costs nothing and a backend regression here is a page-blanking
  bug, not just an empty tab.
- **Results** — file cards once `job.status === "COMPLETED"`; PDF thumbnails render as
  scaled/clipped `<iframe>`s (no pdf.js dependency), other types fall back to a file-type
  icon. Each card sets `overflow: "hidden"` + `minWidth: 0` and the filename `Text` gets
  `wordBreak: "break-word"` — a long unbreakable filename in a narrow `SimpleGrid` column
  used to overflow into the neighboring card at high zoom without these.
  Each card also carries a download `ActionIcon` in its top-right corner. It is an
  `<a download>` **beside** the card's `UnstyledButton`, absolutely positioned over it —
  not nested inside, which would be invalid interactive-inside-interactive markup. It
  points at `FileEntry.downloadUrl`, a second presigning of the same S3 object with
  `Content-Disposition: attachment` (see `list_result_files()` in
  [`packages/survey_shared/AGENTS.md`](../../packages/survey_shared/AGENTS.md)); the
  plain `url` must stay inline or the iframe preview would download instead of render.
  Whether the browser then shows a "save as" dialog or drops the file straight into
  Downloads is the viewer's own setting — a page can't force the picker.
- **Property Metadata** — `MetadataView` renders `job.metadata` (the scraper's
  `overview.json`, opaque/per-county — see
  [`apps/worker/survey_art/AGENTS.md`](../worker/survey_art/AGENTS.md)) as nested tables.
  Notable helpers, all in `App.tsx`:
  - `titleCase(key)` — snake_case → Title Case, with an `ACRONYMS` set (`url`, `id`,
    `sop`, `pdf`) that fully-uppercases those words instead of just capitalizing them.
  - `groupRelatedEntries(entries)` / `RELATED_FIELD_GROUPS` — reorders object entries so
    a defined group (currently `[section, township, range, range_]`) renders adjacent,
    without disturbing the relative order of everything else. Add new groups here rather
    than special-casing them in the render path.
  - `CellValue` — renders a value as a clickable link (`isUrl()` regex-tests for
    `http(s)://`) or falls back to normal text formatting (with `wordBreak`/`whiteSpace:
    pre-wrap` so long values wrap instead of overflowing at high zoom); used everywhere
    a table cell or list item might contain a URL (including `meta.source_urls`-style
    arrays).
  - `RAW_SECTION_KEYS` — top-level metadata sections `MetadataView` skips rendering
    (currently `raw_report_fields`/`raw_property_report_fields`): collected and stored
    for completeness (see [`apps/worker/survey_art/AGENTS.md`](../worker/survey_art/AGENTS.md))
    but not meant for the curated view. Add a key here, don't delete the scraper's data,
    if a future raw/debug section shouldn't show up in the tab.
  - Both `Table` variants (array-of-objects and flat key/value) are wrapped in an
    `overflowX: "auto"` `<div>` — wide tables scroll horizontally inside their own box
    instead of blowing out the page at high browser zoom.
- **Map** — `PropertyMap` component, in priority order: (1) a scraper-captured map
  screenshot (`job.metadata.map.image_url`, an `<img>`, not an iframe — county sites
  commonly block iframe embedding via `X-Frame-Options`, so don't reintroduce an iframe
  embed of a live county map page without checking that first) with a "View on County
  Site" button linking to the live page, (2) a Google Maps embed pinned + zoomed to
  `job.location`, (3) a plain address-based Google Maps search, (4) a "no location yet"
  message.

## Cancelling a job

The search button becomes a cancel action while a job is non-terminal; it calls
`api.cancelJob(jobId)` (`DELETE /api/jobs/{id}`), which stops the underlying Fargate
task server-side — not just a client-side "give up on polling."

## Build / deploy

- Local: `make up` (Vite dev server on :5173, hot reload via mounted `src/`).
- AWS: `make deploy web` — `npm run build` → write `config.json` → `s3 sync` → CloudFront
  invalidation.
