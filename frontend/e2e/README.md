# Browser tests

What proves the product works end to end: sign in, land on Mission Control,
read the nav your role is allowed, drill into a machine, get back out with the
keyboard, sign out.

`vitest` (see `../vitest.config.ts`) covers `lib/` — logic, deliberately no
rendering. These specs cover the other side: a real browser, real navigation,
real focus.

## Running them

```bash
npx playwright install chromium   # once per machine
npx playwright test               # all specs, mocked API, builds and serves the app itself
npx playwright test e2e/auth.spec.ts
npx playwright test --ui          # pick and step through
npx playwright show-report        # after a failing CI-style run
```

Nothing else needs to be running. No backend, no database, no seeded tenant.

Already built? Skip the rebuild:

```bash
E2E_START_CMD="npm run start -- --port 3100" npx playwright test
```

Note that the build must carry `NEXT_PUBLIC_API_URL` (the default command does it
for you) — see below.

### Not `next dev`

The default command builds and serves rather than running the dev server, and
that is not a preference. Under `next dev` on this project, Turbopack panics
every time the browser's HMR channel asks it to rebuild the dashboard route:

```
FATAL: An unexpected Turbopack error occurred.
Failed to write app endpoint /dashboard/page
Caused by: Next.js package not found
  Execution of Project::hmr_version_state failed
```

The dev overlay then reloads the page, so clicks land on a document that is
about to be replaced, React reports a hydration mismatch, and specs fail in
ways that look like product bugs — a logout button that does nothing, a login
that never leaves /login. The same six auth specs take 3.8 minutes of timeouts
served by `next dev` and 11.6 seconds served from a build.

## Why the API is mocked by default

The dashboard fans out to roughly 47 endpoints per three-second poll round,
plus whatever the visible cards fetch for themselves. Running these specs
against a real backend would mean standing up FastAPI and Postgres, seeding a
tenant, and then asserting against whatever numbers that seed happened to
produce. That is a suite that only runs on one laptop, and it is exactly why
this repo has had zero browser tests until now.

So every request is answered by `page.route()` interception from
`support/mock-api.ts`. Two things follow, and both are the point:

- **It runs in CI with Node and a browser and nothing else.** No services to
  start, no fixtures to load, no flake from a slow query.
- **The numbers on screen are known**, so the assertions can be about values —
  "2 running machines", "90m total downtime" — rather than about the page
  merely not being blank. A dashboard that renders every tile as `0` looks
  perfectly healthy in a screenshot test.

What this deliberately does **not** cover is whether the backend actually sends
those shapes. That contract is the backend suites' job (`backend/test_*.py`).
If a read-model changes shape, update the fixture here in the same change —
`support/mock-api.ts` is the one file to touch.

### Two rules inside the mock

1. **An endpoint with no fixture answers 404, never `200 {}` or `200 []`.**
   Every card in this app loads inside `try/catch` and renders nothing on
   failure (the #383 empty-state-on-error work), so a 404 is inert. A blanket
   empty object is not: `OeeSnapshot` reads `s.plant.has_data` and would throw
   on `{}`, taking the whole page down and failing some unrelated spec for a
   reason its name gives no hint of.
2. **404, not 401.** `lib/api.ts handleUnauthorized` treats a 401 on an expired
   token as "session over" and redirects to /login. An endpoint nobody
   fixtured must not be able to log a test out by accident.

Run with `E2E_DEBUG_API=1` to have every unfixtured path printed once.

### Why the app is pointed at a same-origin API path

`lib/api.ts` compiles in `NEXT_PUBLIC_API_URL`, defaulting to
`http://127.0.0.1:8000` — a **different origin** from the app. Cross-origin
fetches carrying `Authorization` and `Content-Type: application/json` trigger a
CORS preflight, and an unanswered preflight fails the request before any mock
gets a say. So `playwright.config.ts` starts the dev server with
`NEXT_PUBLIC_API_URL` pointing at `<baseURL>/__e2e-api`, a path no Next route
serves: same origin, no preflight, and `page.route()` answers all of it. The
`:8000` default is intercepted as well, with permissive CORS headers, so
attaching the suite to a server someone else started still works.

## Running against a real backend

```bash
E2E_LIVE_API=1 E2E_USER=... E2E_PASSWORD=... \
  NEXT_PUBLIC_API_URL=https://your-api.example.com npx playwright test
```

Live mode installs no interception at all and logs in through the real form.
Specs that assert on fixture values (`2 running machines`) skip themselves,
because they are statements about the mocks, not about the product. What still
runs live: login, the redirect guards, the nav rendering, Mission Control being
the default view, and both accessibility scans.

Role gating and the machine cockpit skip in live mode too — the first because
your role is whoever `E2E_USER` is, the second because it drives the fleet by
name.

## Environment variables

| Variable | Default | What it does |
| --- | --- | --- |
| `E2E_LIVE_API` | unset | `1` disables all mocking and talks to a real backend |
| `E2E_USER` / `E2E_PASSWORD` | unset | credentials for a live run; specs skip without them |
| `E2E_BASE_URL` | unset | attach to an already-running app instead of spawning one |
| `E2E_PORT` | `3100` | port the suite builds and serves on |
| `E2E_START_CMD` | `npm run build && npm run start -- --port <port>` | how to start the app under test |
| `E2E_DEBUG_API` | unset | `1` logs every request with no fixture behind it |

Port 3100, not 3000, on purpose: the suite must not silently attach to a dev
server you already have running, because that one was started without the
`NEXT_PUBLIC_API_URL` above and would be talking to a real (or absent) backend.

If CI builds the frontend in an earlier step, that build must export
`NEXT_PUBLIC_API_URL=http://127.0.0.1:3100/__e2e-api` — the value is inlined
into the bundle at build time, so a build without it produces an app that
ignores the mock base.

## What is covered

| Spec | Pins |
| --- | --- |
| `auth.spec.ts` | login stores the session; a rejected login shows the backend's reason; no token bounces to /login; an expired token ends the session on the first 401 — with a CONTROL that a stray 401 on a live token does not; logout clears everything |
| `dashboard.spec.ts` | the manifest-driven nav renders grouped by pack; the topbar title follows the active view; the KPI tiles report real computed numbers |
| `mission-control.spec.ts` | Mission Control is the view the dashboard opens on, with a CONTROL proving the title tracks the view rather than being a banner; pulse and insight counts render |
| `machine-cockpit.spec.ts` | the twin cards summarise the fleet; opening one drills into that machine's detail; Tab stays inside the drawer; Escape closes it and returns focus to the card that opened it |
| `role-gating.spec.ts` | an Operator gets shop-floor views and no admin ones (the whole Admin Pack group disappears), with an Admin CONTROL on the same page; a Supervisor keeps Documents and Costing but loses account administration |
| `accessibility.spec.ts` | axe on /login and the dashboard, failing on any serious or critical impact that is not in the itemised baseline below |

## About the accessibility spec

Axe finds the machine-checkable subset of WCAG: missing labels and alt text,
broken ARIA, insufficient contrast, duplicate ids. That is commonly put at
around a third of real accessibility defects, and the part it cannot see is the
part that matters most here — whether focus order makes sense, whether an alert
is announced, whether a tile updating every three seconds is comprehensible to a
screen reader user. A green run means "no known machine-detectable violations",
not "accessible". The keyboard behaviour it cannot check is tested by hand in
`machine-cockpit.spec.ts`.

### The baseline, and the two defects it is holding

The first run found two serious violations that already existed, so the spec
carries them as an itemised baseline — element by element, not rule by rule. A
blanket "ignore color-contrast" would hide every future contrast bug on every
screen; `color-contrast @ .mt-6` hides exactly one known element.

Both are real defects with one-line fixes, and both are outside this suite:

1. **/login** — `color-contrast` on the "Accounts are created by your
   administrator" footnote. `text-slate-500` (#64748b) on `bg-slate-900`
   (#0f172a) is 3.4:1 where 4.5:1 is required. `text-slate-400` gives 6.3:1.
2. **/dashboard** — `label-title-only` on the tenant switcher `<select>`, which
   carries only `title="Switch company / tenant"`. A title is not a label: it
   never appears for keyboard users and screen readers treat it as a last
   resort. It needs an `aria-label` or a visible label.

The baseline can only shrink. A baselined entry that stops firing ALSO fails the
test, telling you to delete it — which is what stops a baseline from quietly
becoming permanent. So when either fix lands, remove its entry from
`accessibility.spec.ts`.

## Adding a spec

- Open the app with `openDashboard(page, { session, api })` from
  `support/app.ts`. It installs the mocks, plants the session, navigates, and
  waits for the nav — in that order, which matters: navigate before the session
  exists and you land on /login for reasons that look like a product bug.
- Seed a role with `session: { role: "Operator" }`. The dashboard decodes the
  JWT itself and gates the entire nav off its `role` and `tenant` claims, so no
  login round-trip is needed to be somebody.
- Override one endpoint with `api: { "/users": respondWith(401, { detail: "no" }) }`.
  The key `"*"` overrides every path at once.
- Give each test a docstring or a comment naming the failure it pins, and where
  a test would pass with the fix reverted, add a CONTROL case that would not.
